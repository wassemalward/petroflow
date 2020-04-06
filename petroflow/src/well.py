"""Implements Well class."""
# pylint: disable=abstract-method

import warnings
from abc import ABCMeta
from copy import copy, deepcopy
from functools import wraps
from collections import Counter

import numpy as np
import pandas as pd

from .abstract_classes import AbstractWell
from .well_segment import WellSegment
from .exceptions import SkipWellException

from .utils import to_list


class SegmentDelegatingMeta(ABCMeta):
    """A metaclass to delegate abstract methods from `Well` to its children
    (instances of `Well` or `WellSegment`)."""
    def __new__(mcls, name, bases, namespace):
        abstract_methods = [
            base.__abstractmethods__ for base in bases if hasattr(base, "__abstractmethods__")
        ]
        abstract_methods = frozenset().union(*abstract_methods)
        for method_name in abstract_methods:
            if method_name not in namespace:
                namespace[method_name] = mcls._make_delegator(method_name)
        return super().__new__(mcls, name, bases, namespace)

    @staticmethod
    def _make_delegator(name):
        @wraps(getattr(WellSegment, name))
        def delegator(self, *args, **kwargs):
            results = []
            for segment in self:
                res = getattr(segment, name)(*args, **kwargs)
                if not isinstance(res, list):
                    res = [res]
                results.extend(res)
            res_well = self.copy()
            res_well.segments = results
            return res_well
        return delegator


def add_segment_properties(cls):
    """Add missing properties of `WellSegment` to `Well`."""
    properties = (WellSegment.attrs_depth_index + WellSegment.attrs_fdtd_index +
                  WellSegment.attrs_no_index + WellSegment.attrs_image)
    for attr in properties:
        if hasattr(cls, attr):
            continue
        def prop(self, attr=attr):
            return getattr(self.aggregated_segment, attr)
        setattr(cls, attr, property(prop))
    return cls


@add_segment_properties
class Well(AbstractWell, metaclass=SegmentDelegatingMeta):
    """A class, representing a well.

    A well consists of segments - instances of `WellSegment` class,
    representing a contiguous part of a well. Initially an instance of `Well`
    class has a single segment, containing information about the whole well.
    Subsequently, several `Well` methods, such as `crop`, `random_crop` or
    `drop_nans`, increase the number of segments, storing them in a tree-based
    structure.

    All methods, declared in `AbstractWell` and not overridden in `Well` will
    be created by `SegmentDelegatingMeta` metaclass. These methods will
    delegate the call to each segment of the well.

    Parameters
    ----------
    path : str or None
        If `None`, `Well` will be created from `segments`. If `str`, a path to
        a directory with well data, containing:
        - `meta.json` - a json dict with the following keys:
            - `name` - well name
            - `field` - field name
            - `depth_from` - minimum depth entry in the well logs (in cm)
            - `depth_to` - maximum depth entry in the well logs (in cm)
          These values will be stored as segment attributes.
        - `samples_dl` and `samples_uv` (optional) - directories, containing
          daylight and ultraviolet images of core samples respectively. Images
          of the same sample must have the same name in both dirs.
        - Optional `.csv`, `.las` or `.feather` file for certain segment
          attributes (see more details in the `WellSegment.Attributes`
          section).
    core_width : positive float, optional
        The width of core samples in cm. Defaults to 10 cm.
    pixels_per_cm : positive float, optional
        The number of pixels in cm used to determine the loaded width of core
        sample images. Image height is calculated so as to keep the aspect
        ratio. Defaults to 5 pixels.
    validate : bool, optional
        Specifies whether to check well data for correctness and consistency.
        Slightly reduces processing speed. Defaults to `True`.
    segments : list of `WellSegment` or `Well` instances or None, optional
        Segments to put into `segments` attribute. Usually is used by methods
        which increase the tree depth. If `None`, `path` must be defined.

    Attributes
    ----------
    segments : list of `WellSegment` or `Well` instances or None
        A segment tree of the well. All leave nodes are instances of
        `WellSegment` class, representing a contiguous part of a well. All
        other tree nodes are instances of `Well` class.
    """
    def __init__(self, *args, segments=None, **kwargs):
        super().__init__()
        if segments is None:
            self.segments = [WellSegment(*args, **kwargs)]
        else:
            self.segments = segments

    @property
    def name(self):
        """str: Well name."""
        return self.segments[0].name

    @property
    def field(self):
        """str: Field name."""
        return self.segments[0].field

    @property
    def tree_depth(self):
        """positive int: Depth of the tree consisting of `Well` and
        `WellSegment` instances. Initial depth of a created `Well` is 2: a
        root and a single segment.
        """
        if all(isinstance(item, WellSegment) for item in self):
            return 2
        return self.segments[0].tree_depth + 1

    @property
    def length(self):
        """float: Length of the well in centimeters."""
        return self.depth_to - self.depth_from

    @property
    def depth_from(self):
        """float: Top of the well in centimeters."""
        return min([well.depth_from for well in self])

    @property
    def depth_to(self):
        """float: Bottom of the well in centimeters."""
        return max([well.depth_to for well in self])

    @property
    def n_segments(self):
        """int: Total number of `WellSegment` instances at the last level of
        the segment tree."""
        return len(self.iter_level())

    @property
    def aggregated_segment(self):
        """WellSegment: The only segment of an aggregated copy of the well."""
        return self.deepcopy().aggregate().segments[0]

    def __iter__(self):
        """Iterate over segments."""
        for segment in self.segments:
            yield segment

    def iter_level(self, level=-1):
        """Iterate over segments at some fixed level of the segment tree.

        Parameters
        ----------
        level : int
            Level of the tree to iterate over.

        Returns
        -------
        segments : list of WellSegment or Well
            Segments from given level.
        """
        level = level if level >= 0 else self.tree_depth + level
        if (level < 0) or (level > self.tree_depth):
            raise ValueError("Level ({}) can't be negative or exceed tree depth ({})".format(level, self.tree_depth))
        if level == 0:
            return [self]
        if level == 1:
            return self.segments
        return [item for well in self for item in well.iter_level(level - 1)]

    def _prune(self):
        """Recursively prune segment tree."""
        self.segments = [well for well in self if isinstance(well, WellSegment) or well.n_segments > 0]
        for well in self:
            if isinstance(well, Well):
                well._prune() # pylint: disable=protected-access

    def prune(self):
        """Remove subtrees without `WellSegment` instances at the last level
        of the tree.

        Returns
        -------
        self : AbstractWell or a child class
            Self with prunned tree.
        """
        self._prune()
        if not self.segments:
            raise SkipWellException("Empty well after prunning")
        return self

    def copy(self):
        """Perform a shallow copy of an object.

        Returns
        -------
        self : AbstractWell
            Shallow copy.
        """
        return Well(segments=copy([seg.copy() for seg in self.segments]))

    def deepcopy(self):
        """Perform a deep copy of an object.

        Returns
        -------
        self : AbstractWell
            Deep copy.
        """
        return deepcopy(self)

    def dump(self, path):
        """Dump well data. First the well is aggregated and then the resulting
        segment is dumped. Segment attributes are saved in the following
        manner:
        - `name`, `field`, `depth_from` and `depth_to` attributes are saved in
          `meta.json` file.
        - `core_dl` and `core_uv` are not saved. Instead, `samples_dl` and
          `samples_uv` directories are copied if exist.
        - All other attributes are dumped in feather format.

        Parameters
        ----------
        path : str
            A path to a directory, where well dir with the dump will be
            created.

        Returns
        -------
        self : AbstractWell or a child class
            Self unchanged.
        """
        self.aggregated_segment.dump(path)
        return self

    def __getitem__(self, key):
        """Select well logs by mnemonics or slice the well along the wellbore.

        Parameters
        ----------
        key : str, list of str or slice
            - If `key` is `str` or `list` of `str`, preserve only those logs
              in `logs` of each segment, that are in `key`.
            - If `key` is `slice` - perform well slicing along the wellbore.
              The call will be delegated to each segment of the well an only
              segments, overlapping with slicing range will be kept. If both
              `start` and `stop` are in `logs.index` of a segment, then only
              `start` is kept to ensure, that such methods as `crop` always
              return the same number of samples regardless of cropping
              position if crop length is given in centimeters and no less than
              `logs_step`. If only one of the ends of the slice present in the
              index, it is kept in the result contrary to usual python slices.

        Returns
        -------
        well : AbstractWell
            A well with filtered logs or depths.
        """
        results = []
        for segment in self:
            try:
                results.append(segment[key])
            except SkipWellException as err:
                err_msg = str(err)
        if len(results) == 0:
            raise SkipWellException(err_msg)
        res_well = self.copy()
        res_well.segments = results
        return res_well

    def plot(self, *args, aggregate=True, **kwargs):
        """Plot well logs and core images.

        All well logs and core images in daylight and ultraviolet are plotted
        on separate subplots.

        Parameters
        ----------
        plot_core : bool
            Specifies whether to plot core images or not. Defaults to `True`.
        interactive : bool
            Specifies whether to draw a plot directly inside a Jupyter
            notebook. Defaults to `True`.
        aggregate : bool
            Specifies whether to plot all segments of the well on the same
            plot or create a separate plot for each segment. Defaults to
            `True`.
        subplot_height : positive int
            Height of each subplot with well log or core samples images in
            pixels. Defaults to 700.
        subplot_width : positive int
            Width of each subplot with well log or core samples images in
            pixels. Defaults to 200.

        Returns
        -------
        self : AbstractWell
            Self unchanged.
        """
        segments = [self.aggregated_segment] if aggregate else self.iter_level()
        for segment in segments:
            segment.plot(*args, **kwargs)
        return self

    def plot_matching(self, *args, aggregate=True, **kwargs):
        """Plot well log and the corresponding core log for each boring
        sequence.

        This method can be used to illustrate the results of core-to-log
        matching.

        Parameters
        ----------
        mode : str or list of str
            Specify type of well log and core log or property to plot. If
            `str`, the same mode will be used for all boring sequences. If
            `list` of `str`, than each boring sequence will have its own mode.
            In this case length of the `list` should match the number of
            boring sequences.

            Each mode has the same structure as `mode` in `match_core_logs`.
            If `None` and core-to-log matching was performed beforehand,
            chosen matching modes are used.
            Defaults to `None`.
        scale : bool
            Specifies whether to lineary scale core log values to well log
            values. Defaults to `False`.
        interactive : bool
            Specifies whether to draw a plot directly inside a Jupyter
            notebook. Defaults to `True`.
        aggregate : bool
            Specifies whether to plot all segments of the well on the same
            plot or create a separate plot for each segment. Defaults to
            `True`.
        subplot_height : positive int
            Height of each subplot with well and core logs. Defaults to 700.
        subplot_width : positive int
            Width of each subplot with well and core logs. Defaults to 200.

        Returns
        -------
        self : AbstractWell
            Self unchanged.
        """
        segments = [self.aggregated_segment] if aggregate else self.iter_level()
        for segment in segments:
            segment.plot_matching(*args, **kwargs)
        return self

    def drop_layers(self, layers, connected=True):
        """Drop layers, whose names match any pattern from `layers`.

        Parameters
        ----------
        layers : str or list of str
            Regular expressions, specifying layer names to drop.
        connected : bool, optional
            Specifies whether to join segments with kept layers, that go one
            after another. Defaults to `True`.

        Returns
        -------
        well : Well
            The well, whose segments represent kept layers.
        """
        for well in self.iter_level(-2):
            well.segments = [Well(segments=segment.drop_layers(layers, connected)) for segment in well]
        return self.prune()

    def keep_layers(self, layers, connected=True):
        """Drop layers, whose names don't match any pattern from `layers`.

        Parameters
        ----------
        layers : str or list of str
            Regular expressions, specifying layer names to keep.
        connected : bool, optional
            Specifies whether to join segments with kept layers, that go one
            after another. Defaults to `True`.

        Returns
        -------
        well : Well
            The well, whose segments represent kept layers.
        """
        for well in self.iter_level(-2):
            well.segments = [Well(segments=segment.keep_layers(layers, connected)) for segment in well]
        return self.prune()

    def keep_matched_sequences(self, mode=None, threshold=0.6):
        """Keep boring sequences, matched using given `mode` with `R^2`
        greater than `threshold`.

        Parameters
        ----------
        mode : str or list of str
            Chosen matching mode to keep a sequence. It has the same structure
            as `mode` in `match_core_logs`.
        threshold : float
            Minimum value of `R^2` to keep a sequence.

        Returns
        -------
        self : AbstractWell or a child class
            The well with kept matched segments.
        """
        for well in self.iter_level(-2):
            well.segments = [
                Well(segments=segment.keep_matched_sequences(mode, threshold)) for segment in well
            ]
        return self.prune()

    def create_segments(self, src, connected=True, length=None):
        """Split segments at the last level of the segment tree into parts
        with depth ranges, specified in attributes in `src`.

        Parameters
        ----------
        src : str or iterable
            Names of attributes to get depths ranges for splitting. If `src`
            consists of attributes in fdtd format then each row will represent
            a new segment. Otherwise, an exception will be raised.
        connected : bool, optional
            Join segments which are one after another. Defaults to `True`.

        Returns
        -------
        self : AbstractWell or a child class
            The well with split segments.
        """
        wells = self.iter_level(-2)
        for well in wells:
            well.segments = [
                Well(segments=segment.create_segments(src, connected, length)) for segment in well
            ]
        return self.prune()

    def _check_segment_lengths(self, length):
        if any(seg.length < length for seg in self.iter_level()):
            err_msg = "A segment, shorter than {} exists. Call drop_short_segments before random_crop.".format(length)
            raise ValueError(err_msg)

    def crop(self, length, step, drop_last=False, fill_value=0):
        """Create crops from segments at the last level. All cropped segments
        have the same length and are cropped with some fixed step. The tree
        depth will be increased.

        Parameters
        ----------
        length : positive float
            Length of each crop in centimeters.
        step : positive float
            Step of cropping in centimeters.
        drop_last : bool, optional
            If `True`, only crops that lie within well segments will be kept.
            If `False`, the first crop which comes out of segment bounds will
            also be kept to cover the whole segment with crops. Its `logs`
            will be padded by `fill_value` at the end to have given `length`.
            Defaults to `False`.
        fill_value : float, optional
            Value to fill padded part of `logs`. Defaults to 0.

        Returns
        -------
        self : AbstractWell or a child class
            The well with cropped segments.
        """
        if drop_last:
            self._check_segment_lengths(length)
        wells = self.iter_level(-2)
        for well in wells:
            well.segments = [
                Well(segments=segment.crop(length, step, drop_last, fill_value))
                for segment in well
            ]
        return self

    def random_crop(self, length, n_crops=1):
        """Create random crops from segments at the last level. All cropped
        segments have the same length, their positions are sampled uniformly
        from the segment. The tree depth will be increased. Branches of the
        tree without segments at last level will be dropped.

        Parameters
        ----------
        length : positive float
            Length of each crop in centimeters.
        n_crops : positive int, optional
            The number of crops from the well. Defaults to 1.

        Returns
        -------
        self : AbstractWell or a child class
            The well with cropped segments.
        """
        self._check_segment_lengths(length)
        wells = self.iter_level(-2)
        p = np.array([sum([segment.length for segment in item]) for item in wells])
        random_wells = Counter(np.random.choice(wells, n_crops, p=p/sum(p)))
        for well in wells:
            if well in random_wells:
                n_well_crops = random_wells[well]
                p = np.array([item.length for item in well])
                random_segments = Counter(np.random.choice(well.segments, n_well_crops, p=p/sum(p)))
                well.segments = [
                    Well(segments=segment.random_crop(length, n_segment_crops))
                    for segment, n_segment_crops in random_segments.items()
                ]
            else:
                well.segments = []
        return self.prune()

    def drop_nans(self, logs=None):
        """Split a well into contiguous segments, that do not contain `nan`
        values in logs, specified in `logs`. The tree depth will be increased.

        Parameters
        ----------
        logs : None or int or list of str
            - If `None`, create segments without `nan` values in all logs.
            - If `int`, create segments so that each row of logs has at least
              `logs` not-nan values.
            - If `list`, create segments without `nan` values in logs with
              mnemonics in `logs`.
            Defaults to `None`.

        Returns
        -------
        self : AbstractWell
            The well with dropped `nan` values from segments.
        """
        wells = self.iter_level(-2)
        for well in wells:
            well.segments = [Well(segments=segment.drop_nans(logs=logs)) for segment in well]
        return self.prune()

    def drop_short_segments(self, min_length):
        """Drop segments at the last level with length smaller than
        `min_length`.

        Parameters
        ----------
        min_length : positive float
            Minimum length of a segment in centimeters to be kept in the well.

        Returns
        -------
        self : AbstractWell
            The well with dropped short segments.
        """
        wells = self.iter_level(-2)
        for well in wells:
            well.segments = [segment for segment in well if segment.length >= min_length]
        return self.prune()

    def _aggregate_array(self, func, attr):
        """Aggregate loaded attributes from `WellSegment.attrs_image`.

        Parameters
        ----------
        func : {'mean', 'max'}
            Name of an aggregation function.
        attr : str
            Name of an attribute.

        Returns
        -------
        numpy.ndarray
            Assembled array.
        """
        if getattr(self.iter_level()[0], '_' + attr) is None:
            return None
        if func not in ['mean', 'max']:
            warnings.warn("Only 'mean' and 'max' aggregations are currently supported for image attributes, \
                          but {} was given. It was replaced by 'mean'.".format(func))
            func = 'mean'

        pixels_per_cm = self.iter_level()[0].pixels_per_cm
        agg_array_height_pix = round((self.depth_to - self.depth_from) * pixels_per_cm)
        attr_val_shape = getattr(self.iter_level()[0], '_' + attr).shape

        total = np.zeros((agg_array_height_pix, *attr_val_shape[1:]), dtype=int)
        background = np.full_like(total, np.nan, dtype=np.double)
        for segment in self.iter_level():
            attr_val = getattr(segment, '_' + attr)
            segment_place = slice(round((segment.depth_from - self.depth_from) * pixels_per_cm),
                                  round((segment.depth_to - self.depth_from) * pixels_per_cm))

            if func == 'max':
                background[segment_place] = np.fmax(background[segment_place], attr_val)
                continue
            if func == 'mean':
                background[segment_place] = np.nansum([background[segment_place], attr_val], axis=0)
                total[segment_place] += 1

        if func == 'max':
            return background

        total = np.where(total == 0, 1, total)
        return background / total

    def aggregate(self, func="mean", level=0):
        """Aggregate loaded segments' attributes from `WellSegment.attrs_image`
        and `WellSegment.attrs_depth_index`. Concatenate loaded segments'
        attributes from `WellSegment.attrs_fdtd_index`. The result of
        aggregation and concatenation is a single segment for each subtree
        starting at level `level`. `depth_from` and `depth_to` for each of
        these segments will be minimum `depth_from` and maximum `depth_to`
        along all gathered segments of that subtree.

        Parameters
        ----------
        func : str, callable
            Function to use for aggregating the data.
            - `str` - short function name (e.g. `'max'`, `'min'`). See
              `pd.aggregate` documentation.
            - `callable` - a function which takes a `pd.Series` and returns a
               single element.
            Only 'mean' and 'max' aggregations are currently supported for
            attributes from `WellSegment.attrs_image`!
            Defaults to "mean".
        level : int, optional
            The level of the well tree to which aggregation is performed. All
            segments of each subtree on this level will be gathered into one.
            Defaults to an aggregation of the whole tree.

        Returns
        -------
        self : AbstractWell
            The well with gathered segments on level `level`.
        """
        if level < -self.tree_depth or level == -1 or level >= self.tree_depth - 1:
            raise ValueError("Aggregation level can't be ({})".format(level))

        aggregate_attrs = list(WellSegment.attrs_depth_index)
        concat_attrs = list(WellSegment.attrs_fdtd_index)

        wells = self.iter_level(level)
        for well in wells:
            well.segments = [seg[:seg.actual_depth_to] for seg in well.iter_level()]
            seg_0 = well.segments[0]

            # TODO: different aggregation functions
            for attr in WellSegment.attrs_image:
                setattr(seg_0, "_" + attr, well._aggregate_array(func, attr))  # pylint: disable=protected-access

            # Concatenate all segments' attributes
            for attr in aggregate_attrs + concat_attrs:
                attr_values = [getattr(segment, "_" + attr) for segment in well.segments]
                if all(value is None for value in attr_values):
                    if attr in concat_attrs:
                        concat_attrs.remove(attr)
                    else:
                        aggregate_attrs.remove(attr)
                    continue
                # If an attribute is not loaded for several segments, it should be loaded explicitly.
                # It can happen in case of previous manual processing of a `Well`.
                attr_val_0 = pd.concat([getattr(segment, attr) for segment in well.segments])
                setattr(seg_0, "_" + attr, attr_val_0)

            for attr in concat_attrs:
                attr_val_0 = getattr(seg_0, "_" + attr)
                attr_val_0.reset_index(inplace=True)
                attr_val_0.drop_duplicates(inplace=True)
                attr_val_0.set_index(["DEPTH_FROM", "DEPTH_TO"], inplace=True)
                attr_val_0.sort_index(inplace=True)
                setattr(seg_0, "_" + attr, attr_val_0)

            for attr in aggregate_attrs:
                attr_val_0 = getattr(seg_0, '_' + attr)
                attr_val_0 = attr_val_0.groupby(level=0).agg(func)

                # Add NaN values to `logs`.
                if attr == "logs":
                    index = pd.RangeIndex(attr_val_0.index[0], attr_val_0.index[-1] + seg_0.logs_step,
                                          seg_0.logs_step, name="DEPTH")
                    attr_val_0 = attr_val_0.reindex(index=index)
                setattr(seg_0, "_" + attr, attr_val_0)
            setattr(seg_0, "depth_from", well.depth_from)
            setattr(seg_0, "depth_to", well.depth_to)
            well.segments = [seg_0]
        return self.prune()

    def has_attr(self, attrs):
        """Drop segments without specified attrs. If well hasn't some attribute,
        `SkipWellException` will be raised.

        Parameters
        ----------
        attrs : str or list of str
            Attributes which segment must have.

        Returns
        -------
        self : AbstractWell
            The same well if well has all attributes.
        """
        for attr in to_list(attrs):
            if not self.iter_level()[0]._has_file(attr): # pylint: disable=protected-access
                raise SkipWellException("Well hasn't file {}".format(attr))
        return self

    def sample_segments(self, n_segments, skip=False):
        """Uniformly sample segments. If well hasn't enough segments, resulting
        well.

        Parameters
        ----------
        n_segments : positive int
            Number of segments to sample
        skip : bool
            If True, well will be skipped if it hasn't enough segments, else
            well will not be changed.

        Returns
        -------
        self : AbstractWell
            The well with dropped short segments.
        """
        if self.n_segments < n_segments:
            if skip:
                raise SkipWellException("Well hasn't enough segments {}".format(n_segments))
            n_segments = self.n_segments

        wells = self.iter_level(-2)
        segments = [(i, segment) for i, well in enumerate(wells) for segment in well]
        random_segments = [segments[i] for i in np.random.choice(len(segments), n_segments, False)]
        for well in wells:
            well.segments = []
        for i, segment in random_segments:
            wells[i].segments.append(segment)
        return self.prune()
