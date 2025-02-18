from __future__ import annotations

import inspect
import types
from typing import TYPE_CHECKING, Any, Callable

import paddle

from ....symbolic.statement_ir import Symbol
from ....utils import (
    ASSERT,
    EventGuard,
    InnerError,
    NameGenerator,
    is_break_graph_api,
    is_break_graph_tensor_methods,
    is_builtin_fn,
    is_paddle_api,
    magic_method_builtin_dispatch,
    psdb_breakpoint,
    psdb_print,
)
from ....utils.exceptions import BreakGraphError, FallbackErrorBase
from ..dispatcher import Dispatcher
from ..guard import (
    StringifyExpression,
    check_guard,
    object_equal_stringify_guard,
    union_free_vars,
)
from ..mutable_data import MutableDictLikeData
from ..tracker import DanglingTracker, DummyTracker, GetAttrTracker, Tracker
from .base import VariableBase, VariableFactory
from .basic import ConstantVariable, PrintStmtVariable

if TYPE_CHECKING:
    from ..function_graph import FunctionGraph


class CallableVariable(VariableBase):
    def __init__(self, graph: FunctionGraph, tracker: Tracker):
        super().__init__(graph, tracker)

    def __call__(self, /, *args, **kwargs) -> VariableBase:
        """Why we need '/' to make self positional only?

        If kwargs have {'self': xxx}, this function call raise a error.
        See: test_str_format.py for details.
        """
        return self.call_function(*args, **kwargs)

    def call_function(self, /, *args, **kwargs):
        raise NotImplementedError("call_function is not implemented.")


class FunctionVariable(CallableVariable):
    def __init__(
        self, fn: Callable[..., Any], graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(graph, tracker)
        self.value = fn

    def get_py_value(self, allow_tensor=False):
        return self.value

    def get_code(self) -> types.CodeType:
        return self.value.__code__

    def bind(self, instance: VariableBase, name: str):
        method_var = MethodVariable(
            instance,
            self,
            graph=self.graph,
            tracker=GetAttrTracker(instance, name),
        )
        class_var = VariableFactory.from_value(
            instance.get_py_type(),
            graph=self.graph,
            tracker=GetAttrTracker(instance, "__class__"),
        )
        assert class_var is not None
        self.tracker = GetAttrTracker(class_var, name)
        return method_var

    make_stringify_guard = object_equal_stringify_guard


class UserDefinedFunctionVariable(FunctionVariable):
    def __init__(
        self, fn: Callable[..., Any], graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(fn, graph, tracker)

    def handle_psdb_function(self, /, *args, **kwargs):
        # special function for inner debug.
        if self.value is ASSERT:
            # TODO: add comptime check mechanism
            return ConstantVariable.wrap_literal(
                self.value(args[0].value), self.graph
            )
        if self.value is psdb_print:
            sot_prefix = ConstantVariable.wrap_literal("[SOT]", self.graph)
            self.graph.add_print_variables(
                PrintStmtVariable(([sot_prefix, *args], kwargs), self.graph)
            )
            return ConstantVariable.wrap_literal(None, self.graph)

        if self.value is psdb_breakpoint:
            # do nothing. just return None.
            return ConstantVariable.wrap_literal(None, self.graph)
        return None

    def call_function(self, /, *args, **kwargs) -> VariableBase:
        from ..opcode_inline_executor import OpcodeInlineExecutor

        result = self.handle_psdb_function(*args, **kwargs)
        if result is not None:
            return result

        checkpoint = self.graph.save_memo()
        try:
            inline_executor = OpcodeInlineExecutor(self, *args, **kwargs)
            with EventGuard(
                f"Inline Call: {inline_executor._code.co_name.replace('<', '(').replace('>', ')')}, file {inline_executor._code.co_filename}, line {int(inline_executor._code.co_firstlineno)}"
            ):
                output = inline_executor.inline_call()
        except FallbackErrorBase as e:
            self.graph.restore_memo(checkpoint)
            raise BreakGraphError(
                f"{self.value} is raise a inline call error. {e}"
            )
        return output

    @VariableFactory.register_from_value()
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if isinstance(value, (types.FunctionType)):
            return UserDefinedFunctionVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__name__,
        }


class PaddleApiVariable(FunctionVariable):
    """
    PaddleApiVariable is a subclass of FunctionVariable used to wrap a paddlepaddle API function.

    Args:
        fn (Callable[..., Any]): The paddlepaddle API to be wrapped.
        graph(FunctionGraph): The FunctionGraph object that this variable is associated with.
        tracker(Tracker): The Tracker object that tracks the information of this variable.
    """

    def __init__(
        self, fn: Callable[..., Any], graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(fn, graph, tracker)

    def call_function(self, /, *args, **kwargs):
        if is_break_graph_api(self.value):
            raise BreakGraphError(
                f"breakgraph by unsupport function: {self.value.__name__}"
            )
        return self.graph.call_paddle_api(self.value, *args, **kwargs)

    @VariableFactory.register_from_value(
        successor="UserDefinedFunctionVariable"
    )
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if callable(value) and is_paddle_api(value):
            return PaddleApiVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__name__,
        }

    make_stringify_guard = object_equal_stringify_guard


class TensorFunctionVariable(FunctionVariable):
    def __init__(
        self, method_name: str, graph: FunctionGraph, tracker: Tracker
    ):
        fn = getattr(paddle.static.Variable, method_name)
        super().__init__(fn, graph, tracker)
        self.method_name = method_name

    def call_function(self, /, *args, **kwargs):
        if is_break_graph_tensor_methods(self.method_name):
            raise BreakGraphError()
        return self.graph.call_tensor_method(self.method_name, *args, **kwargs)

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__name__,
        }


class MethodVariable(CallableVariable):
    def __init__(
        self,
        bound_instance: VariableBase,
        fn: VariableBase,
        graph: FunctionGraph,
        tracker: Tracker,
        *,
        method_name: str | None = None,
    ):
        super().__init__(graph, tracker)
        self.bound_instance = bound_instance
        self.fn = fn
        self.method_name = method_name

    def get_py_value(self, allow_tensor=False):
        return self.fn.get_py_value().__get__(
            self.bound_instance.get_py_value(allow_tensor),
            self.bound_instance.get_py_value(allow_tensor).__class__,
        )

    def _reconstruct(self, pycode_gen):
        assert self.method_name is not None
        self.tensor.reconstruct(pycode_gen)
        pycode_gen.gen_load_attr(self.method_name)

    def call_function(self, /, *args, **kwargs):
        return self.fn(*(self.bound_instance, *args), **kwargs)

    @staticmethod
    def wrap_method(
        value: types.MethodType,
        *,
        graph: FunctionGraph,
        tracker: Tracker,
        instance: VariableBase | None = None,
        fn: VariableBase | None = None,
        method_name: str | None = None,
    ):
        # NOTE(SigureMo): Since the method_self need method_var as the obj
        # of the tracker, we need to temporarily set the tracker of method_self
        # to DummyTracker, and set it to GetAttrTracker after method_var is created.
        instance_var = (
            VariableFactory.from_value(value.__self__, graph, DanglingTracker())
            if instance is None
            else instance
        )

        fn_var = (
            VariableFactory.from_value(value.__func__, graph, DanglingTracker())
            if fn is None
            else fn
        )

        method_var = MethodVariable(
            instance_var,
            fn_var,
            method_name=method_name,
            graph=graph,
            tracker=tracker,
        )
        if instance is None:
            instance_var.tracker = GetAttrTracker(method_var, "__self__")
        if fn is None:
            fn_var.tracker = GetAttrTracker(method_var, "__func__")
        return method_var

    @VariableFactory.register_from_value()
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if inspect.ismethod(value):
            return MethodVariable.wrap_method(
                value=value, tracker=tracker, graph=graph
            )
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "method": self.method_name,
        }


class LayerVariable(CallableVariable):
    def __init__(
        self, layer: paddle.nn.Layer, graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(graph, tracker)
        self.value = layer
        self.proxy = self.graph.side_effects.get_proxy(
            MutableDictLikeData, self.get_py_value(), self.proxy_getter
        )

    def get_py_value(self, allow_tensor=False):
        return self.value

    @check_guard
    def make_stringify_guard(self) -> list[StringifyExpression]:
        frame_value_tracer = self.tracker.trace_value_from_frame()
        return [
            StringifyExpression(
                f"id({frame_value_tracer.expr}) == {id(self.get_py_value())}",
                union_free_vars(frame_value_tracer.free_vars),
            ),
            StringifyExpression(
                f"{frame_value_tracer.expr}.training == {self.get_py_value().training}",
                union_free_vars(frame_value_tracer.free_vars),
            ),
        ]

    def proxy_getter(self, proxy: MutableDictLikeData, name: str):
        if not hasattr(proxy.original_data, name):
            return MutableDictLikeData.Empty()

        attr = getattr(proxy.original_data, name)
        if inspect.ismethod(attr) or (
            hasattr(attr, "__self__")
            and inspect.ismethoddescriptor(
                getattr(attr.__self__.__class__, name, None)
            )
        ):
            from .callable import MethodVariable

            fn = None
            if inspect.ismethoddescriptor(
                getattr(attr.__self__.__class__, name, None)
            ):
                class_var = VariableFactory.from_value(
                    self.get_py_type(),
                    self.graph,
                    GetAttrTracker(self, "__class__"),
                )
                fn = VariableFactory.from_value(
                    getattr(attr.__self__.__class__, name),
                    self.graph,
                    GetAttrTracker(class_var, name),
                )
            return MethodVariable.wrap_method(
                value=attr,
                instance=self,
                fn=fn,
                graph=self.graph,
                tracker=GetAttrTracker(self, name),
                method_name=name,
            )

        return VariableFactory.from_value(
            attr, self.graph, tracker=GetAttrTracker(self, name)
        )

    def getattr(self, name: str, default=None):
        if not hasattr(self.value, name):
            if default is not None:
                assert isinstance(default, VariableBase)
                return default
            raise InnerError(
                f"{self.__class__.__name__} {self} has no attribute {name}"
            )
        return self.proxy.get(name)


class UserDefinedLayerVariable(LayerVariable):
    """
    UserDefinedLayerVariable is a subclass of LayerVariable used to wrap a user-defined layer.

    Args:
        layer (paddle.nn.Layer): The user-defined layer to be wrapped.
        graph(FunctionGraph): The FunctionGraph object that this variable is associated with.
        tracker(Tracker): The Tracker object that tracks the information of this variable.
    """

    def __init__(
        self, layer: paddle.nn.Layer, graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(layer, graph, tracker)

    def call_function(self, /, *args, **kwargs):
        fn_var = UserDefinedFunctionVariable(
            self.value.__class__.__call__,
            self.graph,
            GetAttrTracker(self, "__call__"),
        )

        return fn_var(*(self, *args), **kwargs)

    @VariableFactory.register_from_value(successor="PaddleApiVariable")
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if isinstance(value, paddle.nn.Layer):
            return UserDefinedLayerVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__class__.__name__,
        }


class BuiltinVariable(FunctionVariable):
    def __init__(
        self, fn: Callable[..., Any], graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(fn, graph, tracker)
        self.value = fn

    def call_function(self, /, *args, **kwargs):
        # Lookup the handler from dispatcher
        handler = Dispatcher.dispatch(self.value, *args, **kwargs)
        if handler is not None:
            return handler(*args, **kwargs)

        # Try to inline call the magic function
        magic_methods = magic_method_builtin_dispatch(self.value)
        for magic_method in magic_methods:
            sorted_args = args
            if magic_method.is_reverse:
                sorted_args = sorted_args[::-1]
            arg_type = sorted_args[0].get_py_type()
            if hasattr(arg_type, magic_method.name):
                class_fn = getattr(arg_type, magic_method.name)
                class_var = VariableFactory.from_value(
                    arg_type,
                    self.graph,
                    GetAttrTracker(args[0], "__class__"),
                )
                assert isinstance(class_var, VariableBase)
                fn_var = VariableFactory.from_value(
                    class_fn,
                    self.graph,
                    GetAttrTracker(class_var, class_fn.__name__),
                )
                assert isinstance(fn_var, VariableBase)
                return fn_var(*args)

        # Break graph if neither of the above conditions is met
        raise BreakGraphError(
            f"Not support builtin function: {self.value.__name__ if hasattr(self.value, '__name__') else self.value}"
        )

    @VariableFactory.register_from_value()
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if is_builtin_fn(value):
            return BuiltinVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__name__,
        }


class UserDefinedGeneratorVariable(FunctionVariable):
    def __init__(
        self, fn: Callable[..., Any], graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(fn, graph, tracker)

    def call_function(self, /, *args, **kwargs):
        iter_ = self.value()
        var = VariableFactory.from_value(
            iter_, self.graph, DummyTracker([self])
        )
        return var

    @VariableFactory.register_from_value(
        successor="UserDefinedFunctionVariable"
    )
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        if inspect.isgeneratorfunction(value):
            return UserDefinedGeneratorVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {"name": self.value.__name__}


class PaddleLayerVariable(LayerVariable):
    """
    PaddleLayerVariable is a subclass of LayerVariable used to wrap a paddlepaddle layer.

    Args:
        layer (paddle.nn.Layer): The paddle built-in layer to be wrapped.
        graph(FunctionGraph): The FunctionGraph object that this variable is associated with.
        tracker(Tracker): The Tracker object that tracks the information of this variable.
    """

    layer_name_generator = NameGenerator("layer_")

    def __init__(
        self, layer: paddle.nn.Layer, graph: FunctionGraph, tracker: Tracker
    ):
        super().__init__(layer, graph, tracker)
        self.name = self.layer_name_generator.next()

    def __len__(self):
        return len(self.value)

    def len(self):
        return ConstantVariable.wrap_literal(len(self), self.graph)

    def get_symbol(self) -> Symbol:
        return Symbol(self.name)

    def call_function(self, /, *args, **kwargs):
        return self.graph.call_layer(self, *args, **kwargs)

    @VariableFactory.register_from_value(successor="UserDefinedLayerVariable")
    def from_value(value: Any, graph: FunctionGraph, tracker: Tracker):
        # TODO(SigureMo): Add a more common way to check if a value is a paddle builtin layer.
        if isinstance(value, paddle.nn.Layer):
            # If there is a user-defined behavior, such as a container class layer
            # or a hook on the layer, it needs to be converted to UserDefinedLayerVariable,
            # otherwise converted to PaddleLayerVariable
            if (
                isinstance(value, paddle.nn.Sequential)
                or value._forward_pre_hooks
                or value._forward_post_hooks
            ):
                return None
            if value.__module__.startswith("paddle.nn."):
                return PaddleLayerVariable(value, graph, tracker)
        return None

    @property
    def main_info(self) -> dict[str, Any]:
        return {
            "name": self.value.__class__.__name__,
        }
