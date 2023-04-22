# coding=utf-8
# Copyright 2022 The Pix2Seq Authors.
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
"""Contains definitions for the post-activation form of Residual Networks.

Residual networks (ResNets) were proposed in:
[1] Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun
    Deep Residual Learning for Image Recognition. arXiv:1512.03385
"""

import tensorflow as tf
import tensorflow_addons.layers as tfa_layers

BATCH_NORM_EPSILON = 1e-5


class BatchNormRelu(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               relu=True,
               init_zero=False,
               center=True,
               scale=True,
               data_format='channels_last',
               global_bn=True,
               batch_norm_decay=0.9,
               fused=False,
               groups=0,
               **kwargs):
    super(BatchNormRelu, self).__init__(**kwargs)
    self.relu = relu
    self.groups = groups
    if init_zero:
      gamma_initializer = tf.zeros_initializer()
    else:
      gamma_initializer = tf.ones_initializer()
    if data_format == 'channels_first':
      axis = 1
    else:
      axis = -1
    if self.groups > 0:
      self.gn = tfa_layers.GroupNormalization(
          groups=self.groups,
          axis=axis,
          epsilon=BATCH_NORM_EPSILON,
          center=center,
          scale=scale,
          gamma_initializer=gamma_initializer,
          name='gn')
    else:
      if global_bn:
        self.bn = tf.keras.layers.experimental.SyncBatchNormalization(
            axis=axis,
            momentum=batch_norm_decay,
            epsilon=BATCH_NORM_EPSILON,
            center=center,
            scale=scale,
            fused=False,  # note: syncBN doesn't support fuse.
            gamma_initializer=gamma_initializer,
            name='global_bn')
      else:
        self.bn = tf.keras.layers.BatchNormalization(
            axis=axis,
            momentum=batch_norm_decay,
            epsilon=BATCH_NORM_EPSILON,
            center=center,
            scale=scale,
            fused=fused,  # note: fused=True only support 4D input tensors.
            gamma_initializer=gamma_initializer,
            name='bn')

  def call(self, inputs, training):
    if self.groups > 0:
      inputs = self.gn(inputs)
    else:
      inputs = self.bn(inputs, training=training)
    if self.relu:
      inputs = tf.nn.relu(inputs)
    return inputs


class DropBlock(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               keep_prob,
               dropblock_size,
               data_format='channels_last',
               **kwargs):
    self.keep_prob = keep_prob
    self.dropblock_size = dropblock_size
    self.data_format = data_format
    super(DropBlock, self).__init__(**kwargs)

  def call(self, net, training):
    keep_prob = self.keep_prob
    dropblock_size = self.dropblock_size
    data_format = self.data_format
    if not training or keep_prob is None:
      return net

    tf.logging.info(
        'Applying DropBlock: dropblock_size {}, net.shape {}'.format(
            dropblock_size, net.shape))

    if data_format == 'channels_last':
      _, width, height, _ = net.get_shape().as_list()
    else:
      _, _, width, height = net.get_shape().as_list()
    if width != height:
      raise ValueError('Input tensor with width!=height is not supported.')

    dropblock_size = min(dropblock_size, width)
    # seed_drop_rate is the gamma parameter of DropBlcok.
    seed_drop_rate = (1.0 - keep_prob) * width**2 / dropblock_size**2 / (
        width - dropblock_size + 1)**2

    # Forces the block to be inside the feature map.
    w_i, h_i = tf.meshgrid(tf.range(width), tf.range(width))
    valid_block_center = tf.logical_and(
        tf.logical_and(w_i >= int(dropblock_size // 2),
                       w_i < width - (dropblock_size - 1) // 2),
        tf.logical_and(h_i >= int(dropblock_size // 2),
                       h_i < width - (dropblock_size - 1) // 2))

    valid_block_center = tf.expand_dims(valid_block_center, 0)
    valid_block_center = tf.expand_dims(
        valid_block_center, -1 if data_format == 'channels_last' else 0)

    randnoise = tf.random_uniform(net.shape, dtype=tf.float32)
    block_pattern = (
        1 - tf.cast(valid_block_center, dtype=tf.float32) + tf.cast(
            (1 - seed_drop_rate), dtype=tf.float32) + randnoise) >= 1
    block_pattern = tf.cast(block_pattern, dtype=tf.float32)

    if dropblock_size == width:
      block_pattern = tf.reduce_min(
          block_pattern,
          axis=[1, 2] if data_format == 'channels_last' else [2, 3],
          keepdims=True)
    else:
      if data_format == 'channels_last':
        ksize = [1, dropblock_size, dropblock_size, 1]
      else:
        ksize = [1, 1, dropblock_size, dropblock_size]
      block_pattern = -tf.nn.max_pool(
          -block_pattern,
          ksize=ksize,
          strides=[1, 1, 1, 1],
          padding='SAME',
          data_format='NHWC' if data_format == 'channels_last' else 'NCHW')

    percent_ones = (
        tf.cast(tf.reduce_sum((block_pattern)), tf.float32) /
        tf.cast(tf.size(block_pattern), tf.float32))

    net = net / tf.cast(percent_ones, net.dtype) * tf.cast(
        block_pattern, net.dtype)
    return net


class FixedPadding(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self, kernel_size, data_format='channels_last', **kwargs):
    super(FixedPadding, self).__init__(**kwargs)
    self.kernel_size = kernel_size
    self.data_format = data_format

  def call(self, inputs, training):
    kernel_size = self.kernel_size
    data_format = self.data_format
    pad_total = kernel_size - 1
    pad_beg = pad_total // 2
    pad_end = pad_total - pad_beg
    if data_format == 'channels_first':
      padded_inputs = tf.pad(
          inputs, [[0, 0], [0, 0], [pad_beg, pad_end], [pad_beg, pad_end]])
    else:
      padded_inputs = tf.pad(
          inputs, [[0, 0], [pad_beg, pad_end], [pad_beg, pad_end], [0, 0]])

    return padded_inputs


class Conv2dFixedPadding(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               filters,
               kernel_size,
               strides,
               dilation=1,
               data_format='channels_last',
               **kwargs):
    super(Conv2dFixedPadding, self).__init__(**kwargs)
    if strides > 1:
      self.fixed_padding = FixedPadding(kernel_size, data_format=data_format)
    else:
      self.fixed_padding = None
    self.conv2d = tf.keras.layers.Conv2D(
        filters=filters,
        kernel_size=kernel_size,
        strides=strides,
        dilation_rate=(dilation, dilation),
        padding=('SAME' if strides == 1 else 'VALID'),
        use_bias=False,
        kernel_initializer=tf.keras.initializers.VarianceScaling(),
        data_format=data_format,
        name='conv2d')

  def call(self, inputs, training):
    if self.fixed_padding:
      inputs = self.fixed_padding(inputs, training=training)
    return self.conv2d(inputs, training=training)


class IdentityLayer(tf.keras.layers.Layer):

  def call(self, inputs, training):
    return tf.identity(inputs)


class SK_Conv2D(tf.keras.layers.Layer):  # pylint: disable=invalid-name
  """Selective kernel convolutional layer (https://arxiv.org/abs/1903.06586)."""

  def __init__(self,
               filters,
               strides,
               sk_ratio,
               min_dim=32,
               data_format='channels_last',
               groups=0,
               **kwargs):
    super(SK_Conv2D, self).__init__(**kwargs)
    self.data_format = data_format
    self.filters = filters
    self.sk_ratio = sk_ratio
    self.min_dim = min_dim

    # Two stream convs (using split and both are 3x3).
    self.conv2d_fixed_padding = Conv2dFixedPadding(
        filters=2 * filters,
        kernel_size=3,
        strides=strides,
        data_format=data_format,
        name='conv2d_fixed_padding')
    self.batch_norm_relu = BatchNormRelu(data_format=data_format, groups=groups,
                                         name='bn_relu')

    # Mixing weights for two streams.
    mid_dim = max(int(filters * sk_ratio), min_dim)
    self.conv2d_0 = tf.keras.layers.Conv2D(
        filters=mid_dim,
        kernel_size=1,
        strides=1,
        kernel_initializer=tf.keras.initializers.VarianceScaling(),
        use_bias=False,
        data_format=data_format,
        name='conv2d_0')
    self.batch_norm_relu_1 = BatchNormRelu(data_format=data_format,
                                           groups=groups, name='bn_relu1')
    self.conv2d_1 = tf.keras.layers.Conv2D(
        filters=2 * filters,
        kernel_size=1,
        strides=1,
        kernel_initializer=tf.keras.initializers.VarianceScaling(),
        use_bias=False,
        data_format=data_format,
        name='conv2d_1')

  def call(self, inputs, training):
    channel_axis = 1 if self.data_format == 'channels_first' else 3
    pooling_axes = [2, 3] if self.data_format == 'channels_first' else [1, 2]

    # Two stream convs (using split and both are 3x3).
    inputs = self.conv2d_fixed_padding(inputs, training=training)
    inputs = self.batch_norm_relu(inputs, training=training)
    inputs = tf.stack(tf.split(inputs, num_or_size_splits=2, axis=channel_axis))

    # Mixing weights for two streams.
    global_features = tf.reduce_mean(
        tf.reduce_sum(inputs, axis=0), pooling_axes, keepdims=True)
    global_features = self.conv2d_0(global_features, training=training)
    global_features = self.batch_norm_relu_1(global_features, training=training)
    mixing = self.conv2d_1(global_features, training=training)
    mixing = tf.stack(tf.split(mixing, num_or_size_splits=2, axis=channel_axis))
    mixing = tf.nn.softmax(mixing, axis=0)

    return tf.reduce_sum(inputs * mixing, axis=0)


class SE_Layer(tf.keras.layers.Layer):  # pylint: disable=invalid-name
  """Squeeze and Excitation layer (https://arxiv.org/abs/1709.01507)."""

  def __init__(self, filters, se_ratio, data_format='channels_last', **kwargs):
    super(SE_Layer, self).__init__(**kwargs)
    self.data_format = data_format
    self.se_reduce = tf.keras.layers.Conv2D(
        max(1, int(filters * se_ratio)),
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=tf.keras.initializers.VarianceScaling(),
        padding='same',
        data_format=data_format,
        use_bias=True,
        name='se_reduce')
    self.se_expand = tf.keras.layers.Conv2D(
        None,  # This is filled later in build().
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=tf.keras.initializers.VarianceScaling(),
        padding='same',
        data_format=data_format,
        use_bias=True,
        name='se_expand')

  def build(self, input_shape):
    self.se_expand.filters = input_shape[-1]
    super(SE_Layer, self).build(input_shape)

  def call(self, inputs, training):
    spatial_dims = [2, 3] if self.data_format == 'channels_first' else [1, 2]
    se_tensor = tf.reduce_mean(inputs, spatial_dims, keepdims=True)
    se_tensor = self.se_expand(tf.nn.relu(self.se_reduce(se_tensor)))
    return tf.sigmoid(se_tensor) * inputs


class ResidualBlock(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               filters,
               strides,
               dilation=1,
               use_projection=False,
               data_format='channels_last',
               dropblock_keep_prob=None,
               dropblock_size=None,
               sk_ratio=0.,
               se_ratio=0.,
               groups=0,
               **kwargs):
    super(ResidualBlock, self).__init__(**kwargs)
    del dropblock_keep_prob
    del dropblock_size
    self.conv2d_bn_layers = []
    self.shortcut_layers = []
    if dilation > 1:
      raise NotImplementedError('Dilation > 1 not supported in ResidualBlock')
    if use_projection:
      if sk_ratio > 0:  # Use ResNet-D (https://arxiv.org/abs/1812.01187)
        if strides > 1:
          self.shortcut_layers.append(FixedPadding(2, data_format))
        self.shortcut_layers.append(
            tf.keras.layers.AveragePooling2D(
                pool_size=2,
                strides=strides,
                padding='SAME' if strides == 1 else 'VALID',
                data_format=data_format,
                name='shortcut_avgpool2d'))
        self.shortcut_layers.append(
            Conv2dFixedPadding(
                filters=filters,
                kernel_size=1,
                strides=1,
                data_format=data_format,
                name='shortcut_conv2d_fixed_padding'))
      else:
        self.shortcut_layers.append(
            Conv2dFixedPadding(
                filters=filters,
                kernel_size=1,
                strides=strides,
                data_format=data_format,
                name='shortcut_conv2d_fixed_padding'))
      self.shortcut_layers.append(
          BatchNormRelu(relu=False, data_format=data_format, groups=groups,
                        name='bn_relu'))

    self.conv2d_bn_layers.append(
        Conv2dFixedPadding(
            filters=filters,
            kernel_size=3,
            strides=strides,
            data_format=data_format,
            name='conv2d_fixed_padding'))
    self.conv2d_bn_layers.append(BatchNormRelu(data_format=data_format,
                                               groups=groups, name='bn_relu1'))
    self.conv2d_bn_layers.append(
        Conv2dFixedPadding(
            filters=filters, kernel_size=3, strides=1, data_format=data_format,
            name='conv2d_fixed_padding1'))
    self.conv2d_bn_layers.append(
        BatchNormRelu(relu=False, init_zero=True, data_format=data_format,
                      groups=groups, name='bn_relu2'))
    self.se_ratio = se_ratio
    if se_ratio > 0:
      self.se_layer = SE_Layer(filters, se_ratio, data_format=data_format,
                               name='se_layer')

  def call(self, inputs, training):
    shortcut = inputs
    for layer in self.shortcut_layers:
      # Projection shortcut in first layer to match filters and strides
      shortcut = layer(shortcut, training=training)

    for layer in self.conv2d_bn_layers:
      inputs = layer(inputs, training=training)

    if self.se_ratio > 0:
      inputs = self.se_layer(inputs, training=training)

    return tf.nn.relu(inputs + shortcut)


class BottleneckBlock(tf.keras.layers.Layer):
  """BottleneckBlock."""

  def __init__(self,
               filters,
               strides,
               dilation=1,
               use_projection=False,
               data_format='channels_last',
               dropblock_keep_prob=None,
               dropblock_size=None,
               sk_ratio=0.,
               se_ratio=0.,
               groups=0,
               **kwargs):
    super(BottleneckBlock, self).__init__(**kwargs)
    self.projection_layers = []
    if use_projection:
      filters_out = 4 * filters
      if sk_ratio > 0:  # Use ResNet-D (https://arxiv.org/abs/1812.01187)
        if strides > 1:
          self.projection_layers.append(
              FixedPadding(2, data_format, name='proj_fixed_padding'))
        self.projection_layers.append(
            tf.keras.layers.AveragePooling2D(
                pool_size=2,
                strides=strides,
                padding='SAME' if strides == 1 else 'VALID',
                data_format=data_format,
                name='proj_avgpool2d'))
        self.projection_layers.append(
            Conv2dFixedPadding(
                filters=filters_out,
                kernel_size=1,
                strides=1,
                data_format=data_format,
                name='proj_conv2d_fixed_padding'))
      else:
        self.projection_layers.append(
            Conv2dFixedPadding(
                filters=filters_out,
                kernel_size=1,
                strides=strides,
                data_format=data_format,
                name='proj_conv2d_fixed_padding'))
      self.projection_layers.append(
          BatchNormRelu(relu=False, data_format=data_format, groups=groups,
                        name='proj_bn_relu'))
    self.shortcut_dropblock = DropBlock(
        data_format=data_format,
        keep_prob=dropblock_keep_prob,
        dropblock_size=dropblock_size,
        name='shortcut_dropblock')

    self.conv_relu_dropblock_layers = []

    self.conv_relu_dropblock_layers.append(
        Conv2dFixedPadding(filters=filters, kernel_size=1, strides=1,
                           data_format=data_format,
                           name='conv2d_fixed_padding'))
    self.conv_relu_dropblock_layers.append(
        BatchNormRelu(data_format=data_format, groups=groups,
                      name='bn_relu'))
    self.conv_relu_dropblock_layers.append(
        DropBlock(
            data_format=data_format,
            keep_prob=dropblock_keep_prob,
            dropblock_size=dropblock_size,
            name='dropblock'))

    if sk_ratio > 0:
      self.conv_relu_dropblock_layers.append(
          SK_Conv2D(filters, strides, sk_ratio, data_format=data_format,
                    groups=groups, name='sk_conv2d'))
    else:
      self.conv_relu_dropblock_layers.append(
          Conv2dFixedPadding(
              filters=filters,
              kernel_size=3,
              strides=strides,
              dilation=dilation,
              data_format=data_format,
              name='conv2d_fixed_padding1'))
      self.conv_relu_dropblock_layers.append(
          BatchNormRelu(data_format=data_format, groups=groups,
                        name='bn_relu1'))
    self.conv_relu_dropblock_layers.append(
        DropBlock(
            data_format=data_format,
            keep_prob=dropblock_keep_prob,
            dropblock_size=dropblock_size,
            name='dropblock1'))

    self.conv_relu_dropblock_layers.append(
        Conv2dFixedPadding(
            filters=4 * filters,
            kernel_size=1,
            strides=1,
            data_format=data_format,
            name='conv2d_fixed_padding2'))
    self.conv_relu_dropblock_layers.append(
        BatchNormRelu(relu=False, init_zero=True, data_format=data_format,
                      groups=groups, name='bn_relu2'))
    self.conv_relu_dropblock_layers.append(
        DropBlock(
            data_format=data_format,
            keep_prob=dropblock_keep_prob,
            dropblock_size=dropblock_size,
            name='dropblock2'))

    if se_ratio > 0:
      self.conv_relu_dropblock_layers.append(
          SE_Layer(filters, se_ratio, data_format=data_format,
                   name='se_layer'))

  def call(self, inputs, training):
    shortcut = inputs
    for layer in self.projection_layers:
      shortcut = layer(shortcut, training=training)
    shortcut = self.shortcut_dropblock(shortcut, training=training)

    for layer in self.conv_relu_dropblock_layers:
      inputs = layer(inputs, training=training)

    return tf.nn.relu(inputs + shortcut)


class BlockGroup(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               filters,
               block_fn,
               blocks,
               strides,
               data_format='channels_last',
               dropblock_keep_prob=None,
               dropblock_size=None,
               sk_ratio=0.,
               resnet_dc5=False,
               groups=0,
               **kwargs):
    self._name = kwargs.get('name')
    super(BlockGroup, self).__init__(**kwargs)

    strides = 1 if resnet_dc5 else strides
    dilation = 2 if resnet_dc5 else 1

    self.layers = []
    self.layers.append(
        block_fn(
            filters,
            strides,
            use_projection=True,
            data_format=data_format,
            dropblock_keep_prob=dropblock_keep_prob,
            dropblock_size=dropblock_size,
            sk_ratio=sk_ratio,
            groups=groups,
            name='block1'))

    for k in range(1, blocks):
      self.layers.append(
          block_fn(
              filters,
              1,
              dilation=dilation,
              data_format=data_format,
              dropblock_keep_prob=dropblock_keep_prob,
              dropblock_size=dropblock_size,
              sk_ratio=sk_ratio,
              groups=groups,
              name='block%d'%(k+1)))

  def call(self, inputs, training):
    for layer in self.layers:
      inputs = layer(inputs, training=training)
    return tf.identity(inputs, self._name)


class Resnet(tf.keras.layers.Layer):  # pylint: disable=missing-docstring

  def __init__(self,
               block_fn,
               layers,
               width_multiplier,
               cifar_stem=False,
               data_format='channels_last',
               dropblock_keep_probs=None,
               dropblock_size=None,
               sk_ratio=0.,
               variant='standard',
               groups=0,
               **kwargs):
    super(Resnet, self).__init__(**kwargs)
    self.data_format = data_format
    self.variant = variant
    if dropblock_keep_probs is None:
      dropblock_keep_probs = [None] * 4
    if not isinstance(dropblock_keep_probs,
                      list) or len(dropblock_keep_probs) != 4:
      raise ValueError('dropblock_keep_probs is not valid:',
                       dropblock_keep_probs)
    self.initial_conv_relu_max_pool = []
    if cifar_stem:
      self.initial_conv_relu_max_pool.append(
          Conv2dFixedPadding(
              filters=64 * width_multiplier,
              kernel_size=3,
              strides=1,
              data_format=data_format,
              name='stem_conv2d_fixed_padding'))
      self.initial_conv_relu_max_pool.append(IdentityLayer(name='initial_conv'))
      self.initial_conv_relu_max_pool.append(
          BatchNormRelu(data_format=data_format, groups=groups,
                        name='stem_bn_relu'))
      self.initial_conv_relu_max_pool.append(
          IdentityLayer(name='initial_max_pool'))
    else:
      if sk_ratio > 0 or self.variant == 'c3':
        # Use ResNet-D (https://arxiv.org/abs/1812.01187)
        self.initial_conv_relu_max_pool.append(
            Conv2dFixedPadding(
                filters=64 * width_multiplier // 2,
                kernel_size=3,
                strides=1 if self.variant == 'c3' else 2,
                data_format=data_format,
                name='stem_conv2d_fixed_padding'))
        self.initial_conv_relu_max_pool.append(
            BatchNormRelu(data_format=data_format, groups=groups,
                          name='stem_bn_relu1'))
        self.initial_conv_relu_max_pool.append(
            Conv2dFixedPadding(
                filters=64 * width_multiplier // 2,
                kernel_size=3,
                strides=1,
                data_format=data_format,
                name='stem_conv2d_fixed_padding1'))
        self.initial_conv_relu_max_pool.append(
            BatchNormRelu(data_format=data_format, groups=groups,
                          name='stem_bn_relu2'))
        self.initial_conv_relu_max_pool.append(
            Conv2dFixedPadding(
                filters=64 * width_multiplier,
                kernel_size=3,
                strides=1,
                data_format=data_format,
                name='stem_conv2d_fixed_padding2'))
      else:
        self.initial_conv_relu_max_pool.append(
            Conv2dFixedPadding(
                filters=64 * width_multiplier,
                kernel_size=7,
                strides=2,
                data_format=data_format,
                name='stem_conv2d_fixed_padding'))
      self.initial_conv_relu_max_pool.append(IdentityLayer(name='initial_conv'))
      self.initial_conv_relu_max_pool.append(
          BatchNormRelu(data_format=data_format, groups=groups,
                        name='stem_bn_relu'))

      self.initial_conv_relu_max_pool.append(
          tf.keras.layers.MaxPooling2D(
              pool_size=3, strides=2, padding='SAME', data_format=data_format,
              name='stem_maxpooling'))
      self.initial_conv_relu_max_pool.append(
          IdentityLayer(name='initial_max_pool'))

    self.block_groups = []
    self.block_groups.append(
        BlockGroup(
            filters=64 * width_multiplier,
            block_fn=block_fn,
            blocks=layers[0],
            strides=2 if self.variant == 'c3' else 1,
            name='block_group1',
            data_format=data_format,
            dropblock_keep_prob=dropblock_keep_probs[0],
            dropblock_size=dropblock_size,
            sk_ratio=sk_ratio,
            groups=groups))
    self.block_groups.append(
        BlockGroup(
            filters=128 * width_multiplier,
            block_fn=block_fn,
            blocks=layers[1],
            strides=2,
            name='block_group2',
            data_format=data_format,
            dropblock_keep_prob=dropblock_keep_probs[1],
            dropblock_size=dropblock_size,
            sk_ratio=sk_ratio,
            groups=groups))
    if self.variant == 'c3':  # omit the later blocks if c3 variant is used.
      return
    self.block_groups.append(
        BlockGroup(
            filters=256 * width_multiplier,
            block_fn=block_fn,
            blocks=layers[2],
            strides=2,
            name='block_group3',
            data_format=data_format,
            dropblock_keep_prob=dropblock_keep_probs[2],
            dropblock_size=dropblock_size,
            sk_ratio=sk_ratio,
            groups=groups))
    if self.variant == 'c4':  # omit the later block if c4 variant is used.
      return
    self.block_groups.append(
        BlockGroup(
            filters=512 * width_multiplier,
            block_fn=block_fn,
            blocks=layers[3],
            strides=2,
            name='block_group4',
            data_format=data_format,
            dropblock_keep_prob=dropblock_keep_probs[3],
            dropblock_size=dropblock_size,
            sk_ratio=sk_ratio,
            resnet_dc5=self.variant == 'dc5',
            groups=groups))

  def call(self, inputs, training):
    for layer in self.initial_conv_relu_max_pool:
      inputs = layer(inputs, training=training)

    hidden_stack = []
    for _, layer in enumerate(self.block_groups):
      inputs = layer(inputs, training=training)
      hidden_stack.append(inputs)
    if self.data_format == 'channels_last':
      inputs = tf.reduce_mean(inputs, [1, 2])
    else:
      inputs = tf.reduce_mean(inputs, [2, 3])
    inputs = tf.identity(inputs, 'final_avg_pool')
    return hidden_stack, inputs


def resnet(resnet_depth,
           width_multiplier,
           cifar_stem=False,
           data_format='channels_last',
           dropblock_keep_probs=None,
           dropblock_size=None,
           sk_ratio=0.,
           variant='standard',
           groups=0):
  """Returns the ResNet model for a given size and number of output classes."""
  model_params = {
      18: {
          'block': ResidualBlock,
          'layers': [2, 2, 2, 2]
      },
      34: {
          'block': ResidualBlock,
          'layers': [3, 4, 6, 3]
      },
      50: {
          'block': BottleneckBlock,
          'layers': [3, 4, 6, 3]
      },
      101: {
          'block': BottleneckBlock,
          'layers': [3, 4, 23, 3]
      },
      152: {
          'block': BottleneckBlock,
          'layers': [3, 8, 36, 3]
      },
      200: {
          'block': BottleneckBlock,
          'layers': [3, 24, 36, 3]
      }
  }

  if resnet_depth not in model_params:
    raise ValueError('Not a valid resnet_depth:', resnet_depth)

  params = model_params[resnet_depth]
  return Resnet(
      params['block'],
      params['layers'],
      width_multiplier,
      cifar_stem=cifar_stem,
      dropblock_keep_probs=dropblock_keep_probs,
      dropblock_size=dropblock_size,
      data_format=data_format,
      sk_ratio=sk_ratio,
      variant=variant,
      groups=groups,
      name='resnet')
