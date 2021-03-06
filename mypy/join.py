"""Calculation of the least upper bound types (joins)."""

from typing import cast, List

from mypy.types import (
    Type, AnyType, NoneTyp, Void, TypeVisitor, Instance, UnboundType,
    ErrorType, TypeVarType, CallableType, TupleType, ErasedType, TypeList,
    UnionType, FunctionLike, Overloaded
)
from mypy.maptype import map_instance_to_supertype
from mypy.subtypes import is_subtype, is_equivalent, is_subtype_ignoring_tvars


def join_simple(declaration: Type, s: Type, t: Type) -> Type:
    """Return a simple least upper bound given the declared type."""

    if isinstance(s, AnyType):
        return s

    if isinstance(s, NoneTyp) and not isinstance(t, Void):
        return t

    if isinstance(s, ErasedType):
        return t

    if is_subtype(s, t):
        return t

    if is_subtype(t, s):
        return s

    if isinstance(declaration, UnionType):
        return UnionType.make_simplified_union([s, t])

    value = t.accept(TypeJoinVisitor(s))

    if value is None:
        # XXX this code path probably should be avoided.
        # It seems to happen when a line (x = y) is a type error, and
        # it's not clear that assuming that x is arbitrary afterward
        # is a good idea.
        return declaration

    if declaration is None or is_subtype(value, declaration):
        return value

    return declaration


def join_types(s: Type, t: Type) -> Type:
    """Return the least upper bound of s and t.

    For example, the join of 'int' and 'object' is 'object'.

    If the join does not exist, return an ErrorType instance.
    """
    if isinstance(s, AnyType):
        return s

    if isinstance(s, NoneTyp) and not isinstance(t, Void):
        return t

    if isinstance(s, ErasedType):
        return t

    # Use a visitor to handle non-trivial cases.
    return t.accept(TypeJoinVisitor(s))


class TypeJoinVisitor(TypeVisitor[Type]):
    """Implementation of the least upper bound algorithm.

    Attributes:
      s: The other (left) type operand.
    """

    def __init__(self, s: Type) -> None:
        self.s = s

    def visit_unbound_type(self, t: UnboundType) -> Type:
        if isinstance(self.s, Void) or isinstance(self.s, ErrorType):
            return ErrorType()
        else:
            return AnyType()

    def visit_union_type(self, t: UnionType) -> Type:
        if is_subtype(self.s, t):
            return t
        else:
            return UnionType(t.items + [self.s])

    def visit_error_type(self, t: ErrorType) -> Type:
        return t

    def visit_type_list(self, t: TypeList) -> Type:
        assert False, 'Not supported'

    def visit_any(self, t: AnyType) -> Type:
        return t

    def visit_void(self, t: Void) -> Type:
        if isinstance(self.s, Void):
            return t
        else:
            return ErrorType()

    def visit_none_type(self, t: NoneTyp) -> Type:
        if not isinstance(self.s, Void):
            return self.s
        else:
            return self.default(self.s)

    def visit_erased_type(self, t: ErasedType) -> Type:
        return self.s

    def visit_type_var(self, t: TypeVarType) -> Type:
        if isinstance(self.s, TypeVarType) and (cast(TypeVarType, self.s)).id == t.id:
            return self.s
        else:
            return self.default(self.s)

    def visit_instance(self, t: Instance) -> Type:
        if isinstance(self.s, Instance):
            return join_instances(t, cast(Instance, self.s))
        elif isinstance(self.s, FunctionLike):
            return join_types(t, self.s.fallback)
        else:
            return self.default(self.s)

    def visit_callable_type(self, t: CallableType) -> Type:
        # TODO: Consider subtyping instead of just similarity.
        if isinstance(self.s, CallableType) and is_similar_callables(
                t, cast(CallableType, self.s)):
            return combine_similar_callables(t, cast(CallableType, self.s))
        elif isinstance(self.s, Overloaded):
            # Switch the order of arguments to that we'll get to visit_overloaded.
            return join_types(t, self.s)
        else:
            return join_types(t.fallback, self.s)

    def visit_overloaded(self, t: Overloaded) -> Type:
        # This is more complex than most other cases. Here are some
        # examples that illustrate how this works.
        #
        # First let's define a concise notation:
        #  - Cn are callable types (for n in 1, 2, ...)
        #  - Ov(C1, C2, ...) is an overloaded type with items C1, C2, ...
        #  - Callable[[T, ...], S] is written as [T, ...] -> S.
        #
        # We want some basic properties to hold (assume Cn are all
        # unrelated via Any-similarity):
        #
        #   join(Ov(C1, C2), C1) == C1
        #   join(Ov(C1, C2), Ov(C1, C2)) == Ov(C1, C2)
        #   join(Ov(C1, C2), Ov(C1, C3)) == C1
        #   join(Ov(C2, C2), C3) == join of fallback types
        #
        # The presence of Any types makes things more interesting. The join is the
        # most general type we can get with respect to Any:
        #
        #   join(Ov([int] -> int, [str] -> str), [Any] -> str) == Any -> str
        #
        # We could use a simplification step that removes redundancies, but that's not
        # implemented right now. Consider this example, where we get a redundancy:
        #
        #   join(Ov([int, Any] -> Any, [str, Any] -> Any), [Any, int] -> Any) ==
        #       Ov([Any, int] -> Any, [Any, int] -> Any)
        #
        # TODO: Use callable subtyping instead of just similarity.
        result = []  # type: List[CallableType]
        s = self.s
        if isinstance(s, FunctionLike):
            # The interesting case where both types are function types.
            for t_item in t.items():
                for s_item in s.items():
                    if is_similar_callables(t_item, s_item):
                        result.append(combine_similar_callables(t_item, s_item))
            if result:
                # TODO: Simplify redundancies from the result.
                if len(result) == 1:
                    return result[0]
                else:
                    return Overloaded(result)
            return join_types(t.fallback, s.fallback)
        return join_types(t.fallback, s)

    def visit_tuple_type(self, t: TupleType) -> Type:
        if (isinstance(self.s, TupleType) and
                cast(TupleType, self.s).length() == t.length()):
            items = []  # type: List[Type]
            for i in range(t.length()):
                items.append(self.join(t.items[i],
                                       (cast(TupleType, self.s)).items[i]))
            # TODO: What if the fallback types are different?
            return TupleType(items, t.fallback)
        else:
            return self.default(self.s)

    def join(self, s: Type, t: Type) -> Type:
        return join_types(s, t)

    def default(self, typ: Type) -> Type:
        if isinstance(typ, Instance):
            return object_from_instance(typ)
        elif isinstance(typ, UnboundType):
            return AnyType()
        elif isinstance(typ, Void) or isinstance(typ, ErrorType):
            return ErrorType()
        elif isinstance(typ, TupleType):
            return self.default(typ.fallback)
        elif isinstance(typ, FunctionLike):
            return self.default(typ.fallback)
        elif isinstance(typ, TypeVarType):
            return self.default(typ.upper_bound)
        else:
            return AnyType()


def join_instances(t: Instance, s: Instance) -> Type:
    """Calculate the join of two instance types.

    If allow_interfaces is True, also consider interface-type results for
    non-interface types.

    Return ErrorType if the result is ambiguous.
    """

    if t.type == s.type:
        # Simplest case: join two types with the same base type (but
        # potentially different arguments).
        if is_subtype(t, s) or is_subtype(s, t):
            # Compatible; combine type arguments.
            args = []  # type: List[Type]
            for i in range(len(t.args)):
                args.append(join_types(t.args[i], s.args[i]))
            return Instance(t.type, args)
        else:
            # Incompatible; return trivial result object.
            return object_from_instance(t)
    elif t.type.bases and is_subtype_ignoring_tvars(t, s):
        return join_instances_via_supertype(t, s)
    else:
        # Now t is not a subtype of s, and t != s. Now s could be a subtype
        # of t; alternatively, we need to find a common supertype. This works
        # in of the both cases.
        return join_instances_via_supertype(s, t)


def join_instances_via_supertype(t: Instance, s: Instance) -> Type:
    # Give preference to joins via duck typing relationship, so that
    # join(int, float) == float, for example.
    if t.type._promote and is_subtype(t.type._promote, s):
        return join_types(t.type._promote, s)
    elif s.type._promote and is_subtype(s.type._promote, t):
        return join_types(t, s.type._promote)
    res = s
    mapped = map_instance_to_supertype(t, t.type.bases[0].type)
    join = join_instances(mapped, res)
    # If the join failed, fail. This is a defensive measure (this might
    # never happen).
    if isinstance(join, ErrorType):
        return join
    # Now the result must be an Instance, so the cast below cannot fail.
    res = cast(Instance, join)
    return res


def is_similar_callables(t: CallableType, s: CallableType) -> bool:
    """Return True if t and s are equivalent and have identical numbers of
    arguments, default arguments and varargs.
    """

    return (len(t.arg_types) == len(s.arg_types) and t.min_args == s.min_args
            and t.is_var_arg == s.is_var_arg and is_equivalent(t, s))


def combine_similar_callables(t: CallableType, s: CallableType) -> CallableType:
    arg_types = []  # type: List[Type]
    for i in range(len(t.arg_types)):
        arg_types.append(join_types(t.arg_types[i], s.arg_types[i]))
    # TODO kinds and argument names
    # The fallback type can be either 'function' or 'type'. The result should have 'type' as
    # fallback only if both operands have it as 'type'.
    if t.fallback.type.fullname() != 'builtins.type':
        fallback = t.fallback
    else:
        fallback = s.fallback
    return t.copy_modified(arg_types=arg_types,
                           ret_type=join_types(t.ret_type, s.ret_type),
                           fallback=fallback,
                           name=None)


def object_from_instance(instance: Instance) -> Instance:
    """Construct the type 'builtins.object' from an instance type."""
    # Use the fact that 'object' is always the last class in the mro.
    res = Instance(instance.type.mro[-1], [])
    return res


def join_type_list(types: List[Type]) -> Type:
    if not types:
        # This is a little arbitrary but reasonable. Any empty tuple should be compatible
        # with all variable length tuples, and this makes it possible. A better approach
        # would be to use a special bottom type.
        return NoneTyp()
    joined = types[0]
    for t in types[1:]:
        joined = join_types(joined, t)
    return joined
