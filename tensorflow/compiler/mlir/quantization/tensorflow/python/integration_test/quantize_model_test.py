# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
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
"""Tests for quantize_model."""
from typing import List
import warnings

from absl.testing import parameterized
import numpy as np
import tensorflow  # pylint: disable=unused-import

from tensorflow.compiler.mlir.quantization.tensorflow import quantization_options_pb2 as quant_opts_pb2
from tensorflow.compiler.mlir.quantization.tensorflow.python import quantize_model
from tensorflow.python.eager import def_function
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_spec
from tensorflow.python.framework import test_util
from tensorflow.python.module import module
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.saved_model import loader_impl as saved_model_loader
from tensorflow.python.saved_model import save as saved_model_save
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import tag_constants

# Type aliases for quantization method protobuf enums.
_Method = quant_opts_pb2.QuantizationMethod.Method
_ExperimentalMethod = quant_opts_pb2.QuantizationMethod.ExperimentalMethod


def _contains_quantized_function_call(meta_graphdef):
  """Returns true if the graph def has quantized function call."""
  for func in meta_graphdef.graph_def.library.function:
    if func.signature.name.startswith('quantized_'):
      return True
  return False


def _contains_op(meta_graphdef, op_name):
  """Returns true if the graph def contains the given op."""
  # Check the main graph
  if any(node.op == op_name for node in meta_graphdef.graph_def.node):
    return True
  # Check the graph genederated from user defined functions
  for func in meta_graphdef.graph_def.library.function:
    for node in func.node_def:
      if node.op == op_name:
        return True
  return False


@test_util.run_all_in_graph_and_eager_modes
class QuantizationMethodTest(test.TestCase):
  """Test cases regarding the use of QuantizationMethod proto.

  Run all tests cases in both the graph mode (default in TF1) and the eager mode
  (default in TF2) to ensure support for when TF2 is disabled.
  """

  class SimpleModel(module.Module):

    @def_function.function(input_signature=[
        tensor_spec.TensorSpec(shape=[1, 4], dtype=dtypes.float32)
    ])
    def __call__(self, input_tensor):
      filters = np.random.uniform(low=-1.0, high=1.0, size=(4, 3)).astype('f4')

      out = math_ops.matmul(input_tensor, filters)
      return {'output': out}

  def _simple_model_data_gen(self):
    for _ in range(255):
      yield {
          'input_tensor':
              ops.convert_to_tensor(
                  np.random.uniform(low=0, high=150, size=(1, 4)).astype('f4')),
      }

  def test_static_range_quantization_by_default(self):
    model = self.SimpleModel()

    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    # Use default QuantizationOptions.
    converted_model = quantize_model.quantize(
        input_saved_model_path,
        representative_dataset=self._simple_model_data_gen)

    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    # Indirectly prove that it is performing a static-range quantization
    # by checking that it complains about representative_dataset when it is
    # not provided.
    with self.assertRaisesRegex(ValueError, 'representative_dataset'):
      quantize_model.quantize(input_saved_model_path)

  def test_method_unspecified_raises_value_error(self):
    model = self.SimpleModel()

    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            method=_Method.METHOD_UNSPECIFIED))

    with self.assertRaises(ValueError):
      quantize_model.quantize(
          input_saved_model_path, quantization_options=options)

  def test_invalid_method_raises_value_error(self):
    model = self.SimpleModel()

    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    # Set an invalid value of -1 to QuantizationMethod.method.
    options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(method=-1))

    with self.assertRaises(ValueError):
      quantize_model.quantize(
          input_saved_model_path, quantization_options=options)


class StaticRangeQuantizationTest(test.TestCase, parameterized.TestCase):

  def _any_warning_contains(
      self, substring: str,
      warnings_list: List[warnings.WarningMessage]) -> bool:
    """Returns True if any of the warnings contains a given substring."""
    return any(
        map(lambda warning: substring in str(warning.message), warnings_list))

  @parameterized.named_parameters(
      ('none', None, False),
      ('relu', nn_ops.relu, False),
      ('relu6', nn_ops.relu6, False),
      ('with_bias', None, True),
      ('with_bias_and_relu', nn_ops.relu, True),
      ('with_bias_and_relu6', nn_ops.relu6, True),
  )
  @test_util.run_in_graph_and_eager_modes
  def test_qat_conv_model(self, activation_fn, has_bias):

    class ConvModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(
              name='input', shape=[1, 3, 4, 3], dtype=dtypes.float32),
          tensor_spec.TensorSpec(
              name='filter', shape=[2, 3, 3, 2], dtype=dtypes.float32),
      ])
      def conv(self, input_tensor, filter_tensor):
        q_input = array_ops.fake_quant_with_min_max_args(
            input_tensor, min=-0.1, max=0.2, num_bits=8, narrow_range=False)
        q_filters = array_ops.fake_quant_with_min_max_args(
            filter_tensor, min=-1.0, max=2.0, num_bits=8, narrow_range=False)
        bias = array_ops.constant([0, 0], dtype=dtypes.float32)
        out = nn_ops.conv2d(
            q_input,
            q_filters,
            strides=[1, 1, 2, 1],
            dilations=[1, 1, 1, 1],
            padding='SAME',
            data_format='NHWC')
        if has_bias:
          out = nn_ops.bias_add(out, bias, data_format='NHWC')
        if activation_fn is not None:
          out = activation_fn(out)
        q_out = array_ops.fake_quant_with_min_max_args(
            out, min=-0.3, max=0.4, num_bits=8, narrow_range=False)
        return {'output': q_out}

    model = ConvModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    signature_key = signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY
    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    converted_model = quantize_model.quantize(input_saved_model_path,
                                              [signature_key], tags,
                                              output_directory,
                                              quantization_options)
    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()), [signature_key])

    input_data = np.random.uniform(
        low=-0.1, high=0.2, size=(1, 3, 4, 3)).astype('f4')
    filter_data = np.random.uniform(
        low=-0.5, high=0.5, size=(2, 3, 3, 2)).astype('f4')

    expected_outputs = model.conv(input_data, filter_data)
    got_outputs = converted_model.signatures[signature_key](
        input=ops.convert_to_tensor(input_data),
        filter=ops.convert_to_tensor(filter_data))
    # TODO(b/215633216): Check if the accuracy is acceptable.
    self.assertAllClose(expected_outputs, got_outputs, atol=0.01)

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))

  # Run this test only with the eager mode.
  # TODO(b/234820600): Allow models with variables to work when TF2 is disabled.
  @test_util.run_v2_only
  def test_ptq_model_with_variable(self):

    class ConvModelWithVariable(module.Module):
      """A simple model that performs a single convolution to the input tensor.

      It keeps the filter as a tf.Variable.
      """

      def __init__(self):
        self.filters = variables.Variable(
            random_ops.random_uniform(
                shape=(2, 3, 3, 2), minval=-1., maxval=1.))

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(
              name='input', shape=(1, 3, 4, 3), dtype=dtypes.float32),
      ])
      def __call__(self, x):
        out = nn_ops.conv2d(
            x,
            self.filters,
            strides=[1, 1, 2, 1],
            dilations=[1, 1, 1, 1],
            padding='SAME',
            data_format='NHWC')
        return {'output': out}

    def gen_data():
      for _ in range(255):
        yield {
            'input':
                random_ops.random_uniform(
                    shape=(1, 3, 4, 3), minval=0, maxval=150)
        }

    model = ConvModelWithVariable()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    signature_keys = [signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY]
    tags = {tag_constants.SERVING}
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    converted_model = quantize_model.quantize(
        input_saved_model_path,
        signature_keys,
        tags,
        output_directory,
        quantization_options,
        representative_dataset=gen_data)

    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()), signature_keys)

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))

  @parameterized.named_parameters(
      ('none', None, False, False),
      ('relu', nn_ops.relu, False, False),
      ('relu6', nn_ops.relu6, False, False),
      ('bn', None, False, True),
      ('bn_and_relu', nn_ops.relu, False, True),
      ('with_bias', None, True, False),
      ('with_bias_and_bn', None, True, True),
      ('with_bias_and_bn_and_relu', nn_ops.relu, True, True),
      ('with_bias_and_relu', nn_ops.relu, True, False),
      ('with_bias_and_relu6', nn_ops.relu6, True, False),
  )
  @test_util.run_in_graph_and_eager_modes
  def test_conv_ptq_model(self, activation_fn, has_bias, has_bn):

    class ConvModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 3, 4, 3], dtype=dtypes.float32)
      ])
      def conv(self, input_tensor):
        filters = np.random.uniform(
            low=-10, high=10, size=(2, 3, 3, 2)).astype('f4')
        bias = np.random.uniform(low=0, high=10, size=(2)).astype('f4')
        scale, offset = [1.0, 1.0], [0.5, 0.5]
        mean, variance = scale, offset
        out = nn_ops.conv2d(
            input_tensor,
            filters,
            strides=[1, 1, 2, 1],
            dilations=[1, 1, 1, 1],
            padding='SAME',
            data_format='NHWC')
        if has_bias:
          out = nn_ops.bias_add(out, bias)
        if has_bn:
          # Fusing is supported for non-training case.
          out, _, _, _, _, _ = nn_ops.fused_batch_norm_v3(
              out, scale, offset, mean, variance, is_training=False)
        if activation_fn is not None:
          out = activation_fn(out)
        return {'output': out}

    model = ConvModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    def data_gen():
      for _ in range(255):
        yield {
            'input_tensor':
                ops.convert_to_tensor(
                    np.random.uniform(low=0, high=150,
                                      size=(1, 3, 4, 3)).astype('f4')),
        }

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    converted_model = quantize_model.quantize(
        input_saved_model_path, ['serving_default'],
        tags,
        output_directory,
        quantization_options,
        representative_dataset=data_gen)
    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))
    self.assertFalse(_contains_op(output_meta_graphdef, 'FusedBatchNormV3'))

  @parameterized.named_parameters(
      ('none', None, False, False),
      ('relu', nn_ops.relu, False, False),
      ('relu6', nn_ops.relu6, False, False),
      ('bn', None, False, True),
      ('bn_and_relu', nn_ops.relu, False, True),
      ('with_bias', None, True, False),
      ('with_bias_and_bn', None, True, True),
      ('with_bias_and_bn_and_relu', nn_ops.relu, True, True),
      ('with_bias_and_relu', nn_ops.relu, True, False),
      ('with_bias_and_relu6', nn_ops.relu6, True, False),
  )
  @test_util.run_in_graph_and_eager_modes
  def test_depthwise_conv_ptq_model(self, activation_fn, has_bias, has_bn):

    class DepthwiseConvModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 3, 4, 3], dtype=dtypes.float32)
      ])
      def conv(self, input_tensor):
        filters = np.random.uniform(
            low=-10, high=10, size=(2, 3, 3, 1)).astype('f4')
        bias = np.random.uniform(low=0, high=10, size=(3)).astype('f4')
        scale, offset = [1.0, 1.0, 1.0], [0.5, 0.5, 0.5]
        mean, variance = scale, offset
        out = nn_ops.depthwise_conv2d_native(
            input_tensor,
            filters,
            strides=[1, 2, 2, 1],
            dilations=[1, 1, 1, 1],
            padding='SAME',
            data_format='NHWC')
        if has_bias:
          out = nn_ops.bias_add(out, bias)
        if has_bn:
          # Fusing is supported for non-training case.
          out, _, _, _, _, _ = nn_ops.fused_batch_norm_v3(
              out, scale, offset, mean, variance, is_training=False)
        if activation_fn is not None:
          out = activation_fn(out)
        return {'output': out}

    model = DepthwiseConvModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    def data_gen():
      for _ in range(255):
        yield {
            'input_tensor':
                ops.convert_to_tensor(
                    np.random.uniform(low=0, high=150,
                                      size=(1, 3, 4, 3)).astype('f4')),
        }

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    converted_model = quantize_model.quantize(
        input_saved_model_path, ['serving_default'],
        tags,
        output_directory,
        quantization_options,
        representative_dataset=data_gen)
    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))
    self.assertFalse(_contains_op(output_meta_graphdef, 'FusedBatchNormV3'))

  @parameterized.named_parameters(
      ('none', None, False),
      ('relu', nn_ops.relu, False),
      ('relu6', nn_ops.relu6, False),
      ('with_bias', None, True),
      ('with_bias_and_relu', nn_ops.relu, True),
      ('with_bias_and_relu6', nn_ops.relu6, True),
  )
  @test_util.run_in_graph_and_eager_modes
  def test_matmul_ptq_model(self, activation_fn, has_bias):

    class MatmulModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 4], dtype=dtypes.float32)
      ])
      def matmul(self, input_tensor):
        filters = np.random.uniform(
            low=-1.0, high=1.0, size=(4, 3)).astype('f4')
        bias = np.random.uniform(low=-1.0, high=1.0, size=(3,)).astype('f4')
        out = math_ops.matmul(input_tensor, filters)
        if has_bias:
          out = nn_ops.bias_add(out, bias)
        if activation_fn is not None:
          out = activation_fn(out)
        return {'output': out}

    model = MatmulModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    def data_gen():
      for _ in range(255):
        yield {
            'input_tensor':
                ops.convert_to_tensor(
                    np.random.uniform(low=0, high=5, size=(1, 4)).astype('f4')),
        }

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    converted_model = quantize_model.quantize(
        input_saved_model_path, ['serving_default'],
        tags,
        output_directory,
        quantization_options,
        representative_dataset=data_gen)
    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))

  @test_util.run_in_graph_and_eager_modes
  def test_model_no_representative_sample_shows_warnings(self):

    class SimpleMatmulModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 4], dtype=dtypes.float32)
      ])
      def matmul(self, input_tensor):
        filters = random_ops.random_uniform(shape=(4, 3), minval=-1., maxval=1.)
        bias = random_ops.random_uniform(shape=(3,), minval=-1., maxval=1.)

        out = math_ops.matmul(input_tensor, filters)
        out = nn_ops.bias_add(out, bias)
        return {'output': out}

    model = SimpleMatmulModel()
    input_savedmodel_dir = self.create_tempdir('input').full_path
    output_savedmodel_dir = self.create_tempdir().full_path
    saved_model_save.save(model, input_savedmodel_dir)

    tags = [tag_constants.SERVING]
    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    with warnings.catch_warnings(record=True) as warnings_list:
      converted_model = quantize_model.quantize(
          input_savedmodel_dir,
          ['serving_default'],
          tags,
          output_savedmodel_dir,
          quantization_options,
          # Put no sample into the representative dataset to make calibration
          # impossible.
          representative_dataset=lambda: [])

      self.assertNotEmpty(warnings_list)

      # Warning message should contain the function name.
      self.assertTrue(self._any_warning_contains('matmul', warnings_list))
      self.assertTrue(
          self._any_warning_contains('does not have min or max values',
                                     warnings_list))

    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])
    output_loader = saved_model_loader.SavedModelLoader(output_savedmodel_dir)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    # Model is not quantized because there was no sample data for calibration.
    self.assertFalse(_contains_quantized_function_call(output_meta_graphdef))

  @test_util.run_in_graph_and_eager_modes
  def test_model_with_uncalibrated_subgraph(self):

    class IfModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 4], dtype=dtypes.float32)
      ])
      def model_fn(self, x):
        if math_ops.reduce_sum(x) > 10.0:
          filters = np.random.uniform(
              low=-1.0, high=1.0, size=(4, 3)).astype('f4')
          bias = np.random.uniform(low=-1.0, high=1.0, size=(3,)).astype('f4')
          out = math_ops.matmul(x, filters)
          out = nn_ops.bias_add(out, bias)
          return {'output': out}

        filters = np.random.uniform(
            low=-1.0, high=1.0, size=(4, 3)).astype('f4')
        bias = np.random.uniform(low=-1.0, high=1.0, size=(3,)).astype('f4')
        out = math_ops.matmul(x, filters)
        out = nn_ops.bias_add(out, bias)
        return {'output': out}

    model = IfModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    def data_gen():
      for _ in range(10):
        yield {
            'x':
                ops.convert_to_tensor(
                    np.random.uniform(low=0.0, high=1.0,
                                      size=(1, 4)).astype('f4')),
        }

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.STATIC_RANGE))

    with warnings.catch_warnings(record=True) as warnings_list:
      converted_model = quantize_model.quantize(
          input_saved_model_path, ['serving_default'],
          tags,
          output_directory,
          quantization_options,
          representative_dataset=data_gen)

      self.assertNotEmpty(warnings_list)

      # Warning message should contain the function name. The uncalibrated path
      # is when the condition is true, so 'cond_true' function must be part of
      # the warning message.
      self.assertTrue(self._any_warning_contains('cond_true', warnings_list))
      self.assertFalse(self._any_warning_contains('cond_false', warnings_list))
      self.assertTrue(
          self._any_warning_contains('does not have min or max values',
                                     warnings_list))

    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])
    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))


@test_util.run_all_in_graph_and_eager_modes
class DynamicRangeQuantizationTest(test.TestCase, parameterized.TestCase):
  """Test cases for dynamic range quantization.

  Run all tests cases in both the graph mode (default in TF1) and the eager mode
  (default in TF2) to ensure support for when TF2 is disabled.
  """

  def test_matmul_model(self):

    class MatmulModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 4], dtype=dtypes.float32)
      ])
      def matmul(self, input_tensor):
        filters = np.random.uniform(
            low=-1.0, high=1.0, size=(4, 3)).astype('f4')
        out = math_ops.matmul(input_tensor, filters)
        return {'output': out}

    model = MatmulModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.DYNAMIC_RANGE))

    converted_model = quantize_model.quantize(input_saved_model_path,
                                              ['serving_default'], tags,
                                              output_directory,
                                              quantization_options)
    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    self.assertTrue(_contains_quantized_function_call(output_meta_graphdef))

  def test_conv_model(self):

    class ConvModel(module.Module):

      @def_function.function(input_signature=[
          tensor_spec.TensorSpec(shape=[1, 3, 4, 3], dtype=dtypes.float32)
      ])
      def conv(self, input_tensor):
        filters = np.random.uniform(
            low=-10, high=10, size=(2, 3, 3, 2)).astype('f4')
        bias = np.random.uniform(low=0, high=10, size=(2)).astype('f4')
        out = nn_ops.conv2d(
            input_tensor,
            filters,
            strides=[1, 1, 2, 1],
            dilations=[1, 1, 1, 1],
            padding='SAME',
            data_format='NHWC')
        out = nn_ops.bias_add(out, bias, data_format='NHWC')
        out = nn_ops.relu6(out)
        return {'output': out}

    model = ConvModel()
    input_saved_model_path = self.create_tempdir('input').full_path
    saved_model_save.save(model, input_saved_model_path)

    tags = [tag_constants.SERVING]
    output_directory = self.create_tempdir().full_path

    quantization_options = quant_opts_pb2.QuantizationOptions(
        quantization_method=quant_opts_pb2.QuantizationMethod(
            experimental_method=_ExperimentalMethod.DYNAMIC_RANGE))

    converted_model = quantize_model.quantize(input_saved_model_path,
                                              ['serving_default'], tags,
                                              output_directory,
                                              quantization_options)

    self.assertIsNotNone(converted_model)
    self.assertEqual(
        list(converted_model.signatures._signatures.keys()),
        ['serving_default'])

    output_loader = saved_model_loader.SavedModelLoader(output_directory)
    output_meta_graphdef = output_loader.get_meta_graph_def_from_tags(tags)
    # Currently conv is not supported.
    self.assertFalse(_contains_quantized_function_call(output_meta_graphdef))


if __name__ == '__main__':
  test.main()
