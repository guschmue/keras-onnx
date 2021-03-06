###############################################################################
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
###############################################################################
import numpy as np
from ..proto import keras, is_tf_keras, is_keras_older_than
from ..proto.tfcompat import is_tf2
from ..common import with_variable, k2o_logger
from ..common.onnx_ops import apply_tile
from ..common.onnx_ops import apply_reshape, OnnxOperatorBuilder

from .activation import convert_keras_activation
from .adv_activation import convert_keras_advanced_activation, convert_keras_softmax
from .batch_norm import convert_keras_batch_normalization
from .merge import convert_keras_merge_layer
from .dense import convert_keras_dense
from .dot import convert_keras_dot
from .upsample import convert_keras_upsample_1d, convert_keras_upsample_2d, convert_keras_upsample_3d
from .conv import convert_keras_conv1d, convert_keras_conv2d, convert_keras_conv3d
from .conv import convert_keras_conv_transpose_2d, convert_keras_conv_transpose_3d, convert_keras_depthwise_conv_2d
from .conv import convert_keras_separable_conv1d, convert_keras_separable_conv2d
from .pooling import convert_keras_max_pooling_1d, convert_keras_max_pooling_2d, convert_keras_max_pooling_3d
from .pooling import convert_keras_average_pooling_1d, convert_keras_average_pooling_2d, \
    convert_keras_average_pooling_3d
from .crop import convert_keras_crop_1d, convert_keras_crop_2d, convert_keras_crop_3d
from .zeropad import convert_keras_zero_pad_1d, convert_keras_zero_pad_2d, convert_keras_zero_pad_3d
from .embedding import convert_keras_embed
from .simplernn import convert_keras_simple_rnn
from .gru import convert_keras_gru
from .lstm import convert_keras_lstm
from .bidirectional import convert_bidirectional


def convert_keras_reshape(scope, operator, container):
    iop = operator.raw_operator
    target_shape = [-1 if i_ is None else i_ for i_ in iop.output_shape]
    if target_shape[0] == -1:
        target_shape[0] = 0

    oopb = OnnxOperatorBuilder(container, scope)
    oopb.apply_op_with_output('apply_reshape',
                              operator.input_full_names,
                              operator.output_full_names,
                              name=operator.full_name,
                              desired_shape=target_shape)


def convert_keras_concat(scope, operator, container):
    axis = operator.raw_operator.axis
    if axis < 0:
        axis += len(operator.raw_operator.output.shape)
    oopb = OnnxOperatorBuilder(container, scope)
    oopb.apply_op_with_output('apply_concat',
                              operator.input_full_names,
                              operator.output_full_names,
                              name=operator.full_name,
                              axis=axis)


def convert_keras_flatten(scope, operator, container):
    iop = operator.raw_operator
    shape_len = len(operator.get_input_shape())
    oopb = OnnxOperatorBuilder(container, scope)

    if iop.data_format == 'channels_last' or shape_len < 3:
        oopb.apply_op_with_output('apply_flatten',
                                  operator.input_full_names,
                                  operator.output_full_names,
                                  name=operator.full_name)
    else:
        perm = list(range(2, shape_len))
        perm = [0] + perm + [1]
        input_tensor_name = oopb.apply_transpose(operator.inputs[0].full_name,
                                                 name=operator.full_name + '_transpose',
                                                 perm=perm)
        oopb.apply_op_with_output('apply_flatten',
                                  input_tensor_name,
                                  operator.output_full_names,
                                  name=operator.full_name)


def _apply_not_equal(oopb, target_opset, operator):
    if operator.mask_value is None:
        raise ValueError("Masking value was not properly parsed for layer '{}'".format(operator.full_name))
    if target_opset >= 11:
        equal_out = oopb.add_node('Equal', [operator.inputs[0].full_name,
                                            np.array([operator.mask_value], dtype='float32')],
                                  operator.full_name + 'mask')
        not_o = oopb.add_node('Not', equal_out,
                              name=operator.full_name + '_not')
    else:
        k2o_logger().warning("On converting a model with opset < 11, " +
                             "the masking layer result may be incorrect if the model input is in range (0, 1.0).")
        equal_input_0 = oopb.add_node('Cast', [operator.inputs[0].full_name],
                                      operator.full_name + '_input_cast', to=oopb.int32)
        equal_out = oopb.add_node('Equal', [equal_input_0, np.array([operator.mask_value], dtype='int32')],
                                  operator.full_name + 'mask')
        not_o = oopb.add_node('Not', equal_out,
                              name=operator.full_name + '_not')
    return not_o


def convert_keras_masking(scope, operator, container):
    oopb = OnnxOperatorBuilder(container, scope)
    not_o = _apply_not_equal(oopb, container.target_opset, operator)
    cast_o = oopb.apply_cast(not_o, to=oopb.float, name=operator.full_name + '_cast')
    if operator.output_masks:
        reduce_node = oopb.add_node("ReduceSum",
                                    cast_o[0], keepdims=False, axes=[-1], name=operator.full_name + '_reduced')
        oopb.add_node_with_output("Greater", [reduce_node, np.array(0, dtype=np.float32)],
                                  [operator.output_masks[0].full_name], name=operator.full_name + '_greater')

    reduce_node2 = oopb.add_node("ReduceSum",
                                 cast_o, keepdims=True, axes=[-1], name=operator.full_name + 'reduced2')
    greater_o = oopb.add_node("Greater",
                              [reduce_node2, np.array(0, dtype=np.float32)], name=operator.full_name + '_greater2')
    cast2_o = oopb.apply_cast(greater_o, to=oopb.float, name=operator.full_name + '_cast2')

    oopb.add_node_with_output('Mul', [cast2_o[0], operator.inputs[0].full_name], [operator.outputs[0].full_name],
                              name=operator.outputs[0].full_name)


def convert_keras_permute(scope, operator, container):
    axes = [0] + list(operator.raw_operator.dims)
    oopb = OnnxOperatorBuilder(container, scope)
    oopb.apply_op_with_output('apply_transpose',
                              operator.input_full_names,
                              operator.output_full_names,
                              name=operator.full_name,
                              perm=axes)


def convert_keras_repeat_vector(scope, operator, container):
    op = operator.raw_operator
    input_shape_1 = op.input_shape[1] if hasattr(op, 'input_shape') else operator.inputs[0].type.shape[1]

    intermediate_tensor_name = scope.get_unique_variable_name(operator.inputs[0].full_name + '_reshaped')
    apply_reshape(scope, operator.inputs[0].full_name, intermediate_tensor_name, container,
                  desired_shape=[-1, 1, input_shape_1])

    repeats = [1, int(op.n), 1]
    apply_tile(scope, intermediate_tensor_name, operator.outputs[0].full_name, container, repeats=repeats)


def convert_keras_training_only_layer(scope, operator, container):
    oopb = OnnxOperatorBuilder(container, scope)
    oopb.apply_op_with_output('apply_identity',
                              operator.input_full_names,
                              operator.output_full_names,
                              name=operator.full_name)


_layer = keras.layers
_adv_activations = keras.layers.advanced_activations

keras_layer_to_operator = {
    _layer.UpSampling1D: convert_keras_upsample_1d,
    _layer.UpSampling2D: convert_keras_upsample_2d,
    _layer.UpSampling3D: convert_keras_upsample_3d,
    _layer.BatchNormalization: convert_keras_batch_normalization,

    _adv_activations.LeakyReLU: convert_keras_advanced_activation,
    _adv_activations.ThresholdedReLU: convert_keras_advanced_activation,
    _adv_activations.ELU: convert_keras_advanced_activation,
    _adv_activations.PReLU: convert_keras_advanced_activation,

    _layer.Activation: convert_keras_activation,

    _layer.Conv1D: convert_keras_conv1d,
    _layer.Conv2D: convert_keras_conv2d,
    _layer.Conv3D: convert_keras_conv3d,
    _layer.Conv2DTranspose: convert_keras_conv_transpose_2d,
    _layer.Conv3DTranspose: convert_keras_conv_transpose_3d,
    _layer.DepthwiseConv2D: convert_keras_depthwise_conv_2d,
    _layer.SeparableConv1D: convert_keras_separable_conv1d,
    _layer.SeparableConv2D: convert_keras_separable_conv2d,

    _layer.Add: convert_keras_merge_layer,
    _layer.Multiply: convert_keras_merge_layer,
    _layer.Subtract: convert_keras_merge_layer,
    _layer.Average: convert_keras_merge_layer,
    _layer.Maximum: convert_keras_merge_layer,
    _layer.Minimum: convert_keras_merge_layer,
    _layer.Concatenate: convert_keras_concat,

    _layer.Dense: convert_keras_dense,
    _layer.Dot: convert_keras_dot,
    _layer.Embedding: convert_keras_embed,
    _layer.Masking: convert_keras_masking,

    _layer.MaxPooling1D: convert_keras_max_pooling_1d,
    _layer.MaxPooling2D: convert_keras_max_pooling_2d,
    _layer.MaxPooling3D: convert_keras_max_pooling_3d,
    _layer.GlobalMaxPooling1D: convert_keras_max_pooling_1d,
    _layer.GlobalMaxPooling2D: convert_keras_max_pooling_2d,
    _layer.GlobalMaxPooling3D: convert_keras_max_pooling_3d,
    _layer.AveragePooling1D: convert_keras_average_pooling_1d,
    _layer.AveragePooling2D: convert_keras_average_pooling_2d,
    _layer.AveragePooling3D: convert_keras_average_pooling_3d,
    _layer.GlobalAveragePooling1D: convert_keras_average_pooling_1d,
    _layer.GlobalAveragePooling2D: convert_keras_average_pooling_2d,
    _layer.GlobalAveragePooling3D: convert_keras_average_pooling_3d,

    _layer.Cropping1D: convert_keras_crop_1d,
    _layer.Cropping2D: convert_keras_crop_2d,
    _layer.Cropping3D: convert_keras_crop_3d,

    _layer.ZeroPadding1D: convert_keras_zero_pad_1d,
    _layer.ZeroPadding2D: convert_keras_zero_pad_2d,
    _layer.ZeroPadding3D: convert_keras_zero_pad_3d,

    _layer.Flatten: convert_keras_flatten,
    _layer.Reshape: convert_keras_reshape,
    _layer.Permute: convert_keras_permute,
    _layer.RepeatVector: convert_keras_repeat_vector,

    _layer.AlphaDropout: convert_keras_training_only_layer,
    _layer.Dropout: convert_keras_training_only_layer,
    _layer.GaussianDropout: convert_keras_training_only_layer,
    _layer.GaussianNoise: convert_keras_training_only_layer,
    _layer.SpatialDropout1D: convert_keras_training_only_layer,
    _layer.SpatialDropout2D: convert_keras_training_only_layer,
    _layer.SpatialDropout3D: convert_keras_training_only_layer,

    _layer.SimpleRNN: convert_keras_simple_rnn,
    _layer.GRU: convert_keras_gru,
    _layer.LSTM: convert_keras_lstm,
    _layer.Bidirectional: convert_bidirectional
}

if not is_keras_older_than('2.1.3'):
    keras_layer_to_operator.update({
        _adv_activations.Softmax: convert_keras_softmax
    })

if not is_keras_older_than('2.2.0'):
    keras_layer_to_operator.update({
        _adv_activations.ReLU: convert_keras_advanced_activation,
    })

if is_tf_keras and is_tf2:
    keras_layer_to_operator.update({
        _layer.recurrent.GRU: convert_keras_gru,
        _layer.recurrent.LSTM: convert_keras_lstm,
        _layer.recurrent.SimpleRNN: convert_keras_simple_rnn,
    })
    if hasattr(_layer, 'recurrent_v2'):
        keras_layer_to_operator.update({
            _layer.recurrent_v2.GRU: convert_keras_gru,
            _layer.recurrent_v2.LSTM: convert_keras_lstm,
            _layer.normalization_v2.BatchNormalization: convert_keras_batch_normalization,
        })


@with_variable('loaded')
def static_set_ke2onnx_converters(func_set_converter):
    for ky_, val_ in keras_layer_to_operator.items():
        func_set_converter(ky_, val_)
