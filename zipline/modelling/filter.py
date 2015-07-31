"""
filter.py
"""
from numpy import (
    bool_,
    float64,
    nan,
    nanpercentile,
)
from itertools import chain
from operator import attrgetter

from zipline.errors import (
    BadPercentileBounds,
)
from zipline.modelling.term import (
    SingleInputMixin,
    Term,
    TestingTermMixin,
)
from zipline.modelling.expression import (
    BadBinaryOperator,
    FILTER_BINOPS,
    method_name_for_op,
    NumericalExpression,
)


def concat_tuples(*tuples):
    """
    Concatenate a sequence of tuples into one tuple.
    """
    return tuple(chain(*tuples))


def binary_operator(op):
    """
    Factory function for making binary operator methods on a Filter subclass.

    Returns a function "binary_operator" suitable for implementing functions
    like __and__ or __or__.
    """
    # When combining a Filter with a NumericalExpression, we use this
    # attrgetter instance to defer to the commuted interpretation of the
    # NumericalExpression operator.
    commuted_method_getter = attrgetter(method_name_for_op(op, commute=True))

    def binary_operator(self, other):
        if isinstance(self, NumericalExpression):
            self_expr, other_expr, new_inputs = self.build_binary_op(
                op, other,
            )
            return NumExprFilter(
                "({left}) {op} ({right})".format(
                    left=self_expr,
                    op=op,
                    right=other_expr,
                ),
                new_inputs,
            )
        elif isinstance(other, NumericalExpression):
            # NumericalExpression overrides numerical ops to correctly handle
            # merging of inputs.  Look up and call the appropriate
            # right-binding operator with ourself as the input.
            return commuted_method_getter(other)(self)
        elif isinstance(other, Filter):
            if self is other:
                return NumExprFilter(
                    "x_0 {op} x_0".format(op=op),
                    (self,),
                )
            return NumExprFilter(
                "x_0 {op} x_1".format(op=op),
                (self, other),
            )
        elif isinstance(other, int):  # Note that this is true for bool as well
            return NumExprFilter(
                "x_0 {op} ({constant})".format(op=op, constant=int(other)),
                binds=(self,),
            )
        raise BadBinaryOperator(op, self, other)
    return binary_operator


class Filter(Term):
    """
    A boolean predicate on a universe of Assets.
    """
    domain = None
    dtype = bool_

    clsdict = locals()
    clsdict.update(
        {
            method_name_for_op(op): binary_operator(op)
            for op in FILTER_BINOPS
        }
    )

    def then(self, other):
        """
        Create a new filter by computing `self`, then computing `other` on the
        data that survived the first filter.

        Parameters
        ----------
        other : zipline.modelling.filter.Filter
            The Filter to apply next.

        Returns
        -------
        filter : zipline.modelling.filter.SequencedFilter
            A filter which will compute `self` and then `other`.

        See Also
        --------
        zipline.modelling.filter.SequencedFilter
        """
        return SequencedFilter(self, other)


class NumExprFilter(NumericalExpression, Filter):
    """
    A Filter computed from a numexpr expression.
    """

    def compute_from_arrays(self, arrays, mask):
        """
        Compute our result with numexpr, then apply `mask`.
        """
        numexpr_result = super(NumExprFilter, self).compute_from_arrays(
            arrays,
            mask,
        )
        return numexpr_result & mask


class PercentileFilter(SingleInputMixin, Filter):
    """
    A Filter representing assets falling between percentile bounds of a Factor.

    Parameters
    ----------
    factor : zipline.modelling.factor.Factor
        The factor over which to compute percentile bounds.
    min_percentile : float [0.0, 1.0]
        The minimum percentile rank of an asset that will pass the filter.
    max_percentile : float [0.0, 1.0]
        The maxiumum percentile rank of an asset that will pass the filter.
    """
    window_length = 0

    def __new__(cls, factor, min_percentile, max_percentile):
        return super(PercentileFilter, cls).__new__(
            cls,
            inputs=(factor,),
            min_percentile=min_percentile,
            max_percentile=max_percentile,
        )

    def _init(self, min_percentile, max_percentile, *args, **kwargs):
        self._min_percentile = min_percentile
        self._max_percentile = max_percentile
        return super(PercentileFilter, self)._init(*args, **kwargs)

    @classmethod
    def static_identity(cls, min_percentile, max_percentile, *args, **kwargs):
        return (
            super(PercentileFilter, cls).static_identity(*args, **kwargs),
            min_percentile,
            max_percentile,
        )

    def _validate(self):
        """
        Ensure that our percentile bounds are well-formed.
        """
        if not 0.0 <= self._min_percentile < self._max_percentile <= 100.0:
            raise BadPercentileBounds(
                min_percentile=self._min_percentile,
                max_percentile=self._max_percentile,
            )
        return super(PercentileFilter, self)._validate()

    def compute_from_arrays(self, arrays, mask):
        """
        For each row in the input, compute a mask of all values falling between
        the given percentiles.
        """
        # TODO: Review whether there's a better way of handling small numbers
        # of columns.
        data = arrays[0].astype(float64)
        data[~mask.values] = nan

        # FIXME: np.nanpercentile **should** support computing multiple bounds
        # at once, but there's a bug in the logic for multiple bounds in numpy
        # 1.9.2.  It will be fixed in 1.10.
        # c.f. https://github.com/numpy/numpy/pull/5981
        lower_bounds = nanpercentile(
            data,
            self._min_percentile,
            axis=1,
            keepdims=True,
        )
        upper_bounds = nanpercentile(
            data,
            self._max_percentile,
            axis=1,
            keepdims=True,
        )
        return (lower_bounds <= data) & (data <= upper_bounds)


class SequencedFilter(Filter):
    """
    Term representing sequenced computation of two Filters.

    Parameters
    ----------
    first : zipline.modelling.filter.Filter
        The first filter to compute.
    second : zipline.modelling.filter.Filter
        The second filter to compute.

    Notes
    -----
    In general, users should rarely have to construct SequencedFilter instances
    directly.  Instead, prefer construction via `Filter.then`.

    See Also
    --------
    Filter.then
    """
    window_length = 0

    def __new__(cls, first, then):
        return super(SequencedFilter, cls).__new__(
            cls,
            inputs=concat_tuples((first,), then.inputs),
            then=then,
        )

    def _init(self, then, *args, **kwargs):
        self._then = then
        return super(SequencedFilter, self)._init(*args, **kwargs)

    def _validate(self):
        """
        Ensure that we're actually sequencing filters.
        """
        first, then = self.inputs[0], self._then
        if not isinstance(first, Filter):
            raise TypeError("Expected Filter, got %s" % type(first).__name__)
        if not isinstance(then, Filter):
            raise TypeError("Expected Filter, got %s" % type(then).__name__)
        return super(SequencedFilter, self)._validate()

    @classmethod
    def static_identity(cls, then, *args, **kwargs):
        return (
            super(SequencedFilter, cls).static_identity(*args, **kwargs),
            then,
        )

    def compute_from_arrays(self, arrays, mask):
        """
        Call our second filter on its inputs, masking out any inputs rejected
        by our first filter.
        """
        first_result, then_inputs = arrays[0], arrays[1:]
        return self._then.compute_from_arrays(
            then_inputs,
            mask & first_result,
        )


class TestingFilter(TestingTermMixin, Filter):
    """
    Base class for testing engines that asserts all inputs are correctly
    shaped.
    """
    pass
