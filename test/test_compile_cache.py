import torch

import functorch
from torch.testing._internal.common_utils import run_tests, TestCase

from functorch.compile import aot_function, nop
from functorch.compile import memory_efficient_pointwise_fusion


class TestCompileCache(TestCase):
    def check(self, a, b, aot_fn, fn):
        a_clone = a.clone().detach().requires_grad_(True)
        b_clone = b.clone().detach().requires_grad_(True)
        ref = fn(a, b)
        ref.sum().backward()

        res = aot_fn(a_clone, b_clone)
        res.sum().backward()
        assert torch.allclose(res, ref)
        assert torch.allclose(a.grad, a_clone.grad)
        assert torch.allclose(b.grad, b_clone.grad)

    def test_recompilation_on_broadcast(self):
        def fn(x, bias):
            return x + bias

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()
            start_num_recomps = functorch.compile.num_of_recompilations()
            mem_optimized_fn = memory_efficient_pointwise_fusion(
                fn,
                compiler_name="torchscript_nnc",
                hasher_type=hasher_type,
            )

            a = torch.randn(10, 20, requires_grad=True)
            b = torch.randn(20, requires_grad=True)
            self.check(a, b, mem_optimized_fn, fn)

            a = torch.randn(10, 20, requires_grad=True)
            b = torch.randn(10, 20, requires_grad=True)
            self.check(a, b, mem_optimized_fn, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2

    def test_compilation_for_dynamic_shape(self):
        def fn(x, bias):
            return x + bias

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()
            start_num_recomps = functorch.compile.num_of_recompilations()
            mem_optimized_fn = memory_efficient_pointwise_fusion(
                fn, compiler_name="torchscript_nnc", hasher_type=hasher_type
            )

            for s in range(10, 20):
                a = torch.randn(s, requires_grad=True)
                b = torch.randn(s, requires_grad=True)
                self.check(a, b, mem_optimized_fn, fn)

            for s in range(10, 20):
                a = torch.randn(s, requires_grad=True)
                b = torch.randn(s, requires_grad=True)
                self.check(a, b, mem_optimized_fn, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            if hasher_type == "DynamicShapeHasher":
                assert total_recomps == 1
            elif hasher_type == "StaticShapeHasher":
                assert total_recomps == 10

            for s in range(10, 20):
                a = torch.randn(s, s, requires_grad=True)
                b = torch.randn(s, s, requires_grad=True)
                self.check(a, b, mem_optimized_fn, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            if hasher_type == "DynamicShapeHasher":
                assert total_recomps == 2
            elif hasher_type == "StaticShapeHasher":
                assert total_recomps == 20

    def test_global_cache_no_recompilations(self):
        def f(x, bias):
            return x + bias

        def g(x, bias):
            return aot_function(f, nop, nop, hasher_type="DynamicShapeHasher")(x, bias)

        start_num_recomps = functorch.compile.num_of_recompilations()
        for _ in range(10):
            a = torch.randn(10, 20, requires_grad=True)
            b = torch.randn(10, 20, requires_grad=True)
            self.check(a, b, g, f)

        end_num_recomps = functorch.compile.num_of_recompilations()
        total_recomps = end_num_recomps - start_num_recomps
        assert total_recomps == 1

    def test_multiple_functions(self):
        def f(x, bias):
            return x + bias

        def g(x, y):
            return x * y

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()
            mem_optimized_f = memory_efficient_pointwise_fusion(
                f, compiler_name="torchscript_nnc", hasher_type=hasher_type
            )
            mem_optimized_g = memory_efficient_pointwise_fusion(
                g, compiler_name="torchscript_nnc", hasher_type=hasher_type
            )

            start_num_recomps = functorch.compile.num_of_recompilations()
            a = torch.randn(10, requires_grad=True)
            b = torch.randn(10, requires_grad=True)
            self.check(a, b, mem_optimized_f, f)

            a = torch.randn(10, requires_grad=True)
            b = torch.randn(10, requires_grad=True)
            self.check(a, b, mem_optimized_g, g)

            end_num_recomps = functorch.compile.num_of_recompilations()
            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2

            # Force recompilation for function f and check num of recompilations again
            a = torch.randn(10, 20, requires_grad=True)
            b = torch.randn(10, 20, requires_grad=True)
            self.check(a, b, mem_optimized_f, f)

            end_num_recomps = functorch.compile.num_of_recompilations()
            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 3

    def test_high_number_of_args(self):
        def f(*args):
            res = args[0]
            for arg in args:
                res = res * arg
            return res

        def check(args, mem_optimized_fn, fn):
            args_clone = [arg.clone().detach().requires_grad_(True) for arg in args]
            ref = fn(*args)
            ref.sum().backward()

            res = mem_optimized_fn(*args_clone)
            res.sum().backward()
            assert torch.allclose(res, ref)
            for (arg, arg_clone) in zip(args, args_clone):
                assert torch.allclose(arg.grad, arg_clone.grad)

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()

            aot_autograd_f = aot_function(f, nop, nop, hasher_type=hasher_type)

            args = [torch.randn(10, requires_grad=True) for _ in range(100)]
            check(args, aot_autograd_f, f)


class TestCompileCacheNonTensorArgs(TestCase):
    def check(self, a, b, mem_optimized_fn, fn):
        a_clone = a.clone().detach().requires_grad_(True)
        ref = fn(a, b)
        ref.sum().backward()

        res = mem_optimized_fn(a_clone, b)
        res.sum().backward()
        assert torch.allclose(res, ref)
        assert torch.allclose(a.grad, a_clone.grad)

    def test_simple(self):
        def fn(x, p):
            return x * p

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()

            start_num_recomps = functorch.compile.num_of_recompilations()

            aot_autograd_f = aot_function(fn, nop, nop, hasher_type=hasher_type)

            a = torch.randn(2, 2, requires_grad=True)
            b = 2
            self.check(a, b, aot_autograd_f, fn)

            a = torch.randn(2, 2, requires_grad=True)
            b = 3
            self.check(a, b, aot_autograd_f, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2

    def test_dropout(self):
        def fn(x, prob):
            return torch.nn.functional.dropout(x, p=prob)

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()

            start_num_recomps = functorch.compile.num_of_recompilations()

            aot_autograd_f = aot_function(fn, nop, nop, hasher_type=hasher_type)

            a = torch.randn(2, 2, requires_grad=True)
            b = 0.3
            aot_autograd_f(a, b)

            # Setting the prob to 0. This should cause recompilation.
            a = torch.randn(2, 2, requires_grad=True)
            b = 0
            self.check(a, b, aot_autograd_f, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2

    def test_if_condition(self):
        def fn(x, state: bool):
            if state:
                return torch.sin(x)
            else:
                return torch.cos(x)

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()

            start_num_recomps = functorch.compile.num_of_recompilations()

            aot_autograd_f = aot_function(fn, nop, nop, hasher_type=hasher_type)

            a = torch.randn(2, 2, requires_grad=True)
            b = True
            self.check(a, b, aot_autograd_f, fn)

            a = torch.randn(2, 2, requires_grad=True)
            b = False
            self.check(a, b, aot_autograd_f, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2

    def test_custom(self):
        class Record:
            def __init__(self, name, multiplier):
                self.name = name
                self.multiplier = multiplier

            def __eq__(self, other):
                return self.name == other.name and self.multiplier == other.multiplier

            def __hash__(self):
                return hash((self.name, self.multiplier))

        def fn(x, record):
            return x * record.multiplier

        for hasher_type in ["DynamicShapeHasher", "StaticShapeHasher"]:
            functorch.compile.clear_compile_cache()

            start_num_recomps = functorch.compile.num_of_recompilations()

            aot_autograd_f = aot_function(fn, nop, nop, hasher_type=hasher_type)

            a = torch.randn(2, 2, requires_grad=True)
            b = Record("Foo", 0.5)
            self.check(a, b, aot_autograd_f, fn)

            a = torch.randn(2, 2, requires_grad=True)
            b = Record("Bar", 10.2)
            self.check(a, b, aot_autograd_f, fn)

            end_num_recomps = functorch.compile.num_of_recompilations()

            total_recomps = end_num_recomps - start_num_recomps
            assert total_recomps == 2


if __name__ == "__main__":
    run_tests()
