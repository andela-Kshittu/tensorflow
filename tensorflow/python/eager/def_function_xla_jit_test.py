# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.eager import backprop
from tensorflow.python.eager import context
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import errors
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import control_flow_util
from tensorflow.python.ops import resource_variable_ops
from tensorflow.python.platform import test


class DefFunctionTest(test.TestCase):

  def testAutoclusteringWithTfFunction(self):

    @def_function.function(experimental_compile=False)
    def outer(a, b, c):
      return a * inner(b, c) + c

    @def_function.function(experimental_compile=True)
    def inner(b, c):
      return b + c * b

    i1 = constant_op.constant([1.0, 2.0, 3.0, 4.0, 5.0])
    i2 = constant_op.constant([1.0, 2.0, 3.0, 4.0, 5.0])
    i3 = constant_op.constant([1.0, 2.0, 3.0, 4.0, 5.0])

    with context.collect_graphs(optimized=True) as graphs:
      outer(i1, i2, i3)

    self.assertIn('_XlaRun', [n.op for n in graphs[0].node])

  def testBasic(self):

    def fn(x, a):
      return x + a

    func = def_function.function(fn, experimental_compile=False)
    xla_func = def_function.function(fn, experimental_compile=True)

    inputs = constant_op.constant([1, 2, 2, 3, 3])
    self.assertAllClose([2, 3, 3, 4, 4], func(inputs, 1))
    if not test.is_built_with_rocm():
      # XLA support is not yet enabled for TF ROCm
      self.assertAllClose([2, 3, 3, 4, 4], xla_func(inputs, 1))

  def testBasicInt32(self):

    def fn(x, a):
      return x + a

    xla_func = def_function.function(fn, experimental_compile=True)

    inputs = constant_op.constant([1, 2, 2, 3, 3], dtype=dtypes.int32)
    if not test.is_built_with_rocm():
      # XLA support is not yet enabled for TF ROCm
      self.assertAllClose([2, 3, 3, 4, 4], xla_func(inputs, 1))

  def testDerivative(self):
    if test.is_built_with_rocm():
      return

    def fn(x, a):
      return 2 * x + a

    xla_func = def_function.function(fn, experimental_compile=True)

    with backprop.GradientTape() as tape:
      inputs = constant_op.constant([1., 2., 2., 3., 3.])
      tape.watch(inputs)
      outputs = xla_func(inputs, 1)

    self.assertAllClose([2, 2, 2, 2, 2], tape.gradient(outputs, inputs))

    # pylint: disable=protected-access
    (forward, backward) = xla_func.get_concrete_function(
        inputs, 1)._delayed_rewrite_functions.forward_backward()

    # Check that the must-compile attribute gets correctly propagated to the
    # created derivatives.
    self.assertTrue(backward.function_def.attr['_XlaMustCompile'])
    self.assertTrue(forward.definition.attr['_XlaMustCompile'])

  # Calling function with experimental_compile=True from
  # experimental_compile=False should compile the inner func.
  def testNestedCall(self):

    def fn(x, a):
      return x + a

    xla_func = def_function.function(fn, experimental_compile=True)

    def fn2(x, a):
      return xla_func(x, a)

    func = def_function.function(fn2, experimental_compile=False)

    inputs = constant_op.constant([1, 2, 2, 3, 3])
    if not test.is_built_with_rocm():
      # XLA support is not yet enabled for TF ROCm
      self.assertAllClose([2, 3, 3, 4, 4], func(inputs, 1))

  def testNestedCallUnsupportedOps(self):

    def fn(x):
      return array_ops.unique(x).y

    xla_func = def_function.function(fn, experimental_compile=True)

    def fn2(x):
      return xla_func(x)

    func = def_function.function(fn2, experimental_compile=False)
    inputs = constant_op.constant([1, 2, 2, 3, 3])
    if not test.is_built_with_rocm():
      with self.assertRaisesRegexp(errors.InvalidArgumentError,
                                   'not compilable'):
        func(inputs)

  def testUnsupportedOps(self):

    def fn(x):
      return array_ops.unique(x).y  # Unique is not supported by XLA

    func = def_function.function(fn, experimental_compile=False)
    xla_func = def_function.function(fn, experimental_compile=True)

    inputs = constant_op.constant([1, 2, 2, 3, 3])
    self.assertAllClose([1, 2, 3], func(inputs))
    with self.assertRaisesRegexp(errors.InvalidArgumentError, 'not compilable'):
      xla_func(inputs)

  def testFunctionGradient(self):
    v = resource_variable_ops.ResourceVariable(2.0)

    def fn(x):
      return v * x

    func = def_function.function(fn, experimental_compile=False)
    xla_func = def_function.function(fn, experimental_compile=True)

    def run_and_check(test_func):
      x = constant_op.constant(3.0)
      with backprop.GradientTape() as tape:
        y = test_func(x)
      dy = tape.gradient(y, v)

      self.assertAllClose(6.0, y)
      self.assertAllClose(3.0, dy)

    run_and_check(func)
    if not test.is_built_with_rocm():
      # XLA support is not yet enabled for TF ROCm
      run_and_check(xla_func)

  def testControlFlow(self):

    @def_function.function(experimental_compile=True)
    def f(x):
      assert control_flow_util.GraphOrParentsInXlaContext(
          ops.get_default_graph())
      x = ops.convert_to_tensor(x)

      def body(i, a):
        return i + 1, control_flow_ops.cond(i > 2, lambda: a + (x**2),
                                            lambda: a + 3)

      return control_flow_ops.while_loop(
          lambda i, *_: i < 10,
          body, (constant_op.constant(0), constant_op.constant(3.)),
          maximum_iterations=10)[1]

    @def_function.function(experimental_compile=True)
    def g(x):
      x = ops.convert_to_tensor(x)
      with backprop.GradientTape() as tape:
        tape.watch(x)
        y = f(x)
      return y, tape.gradient(y, x)

    self.assertAllClose(40.0, f(2.0))
    self.assertAllClose([40.0, 28.0], g(2.0))

  def testMethodCompilation(self):
    if test.is_built_with_rocm():
      return

    class C(object):

      @def_function.function(experimental_compile=True)
      def f1(self, x, a):
        return x + a

    inputs = constant_op.constant([1, 2, 2, 3, 3])
    c = C()
    self.assertAllClose([2, 3, 3, 4, 4], c.f1(inputs, 1))

  def testMethodCompilationUnsupportedFunc(self):
    if test.is_built_with_rocm():
      return

    class C(object):

      @def_function.function(experimental_compile=True)
      def f1(self, x):
        return array_ops.unique(x).y

    inputs = constant_op.constant([1, 2, 2, 3, 3])
    c = C()
    with self.assertRaisesRegexp(errors.InvalidArgumentError, 'not compilable'):
      c.f1(inputs)

  def testMustBeConstantPropagation(self):
    if test.is_built_with_rocm():
      return

    @def_function.function(experimental_compile=True)
    def f():
      return constant_op.constant([0, 2, 1], dtype=dtypes.int32)

    @def_function.function(experimental_compile=True)
    def g(a, b):
      return array_ops.transpose(a, b)

    @def_function.function
    def z():
      return g(array_ops.ones([3, 4, 3], dtype=dtypes.float32), f())

    z()


if __name__ == '__main__':
  ops.enable_eager_execution()
  test.main()
