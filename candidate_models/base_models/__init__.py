import glob
import logging
import os
from importlib import import_module

import functools

from brainscore.utils import LazyLoad, fullname
from candidate_models import s3
from candidate_models.utils import UniqueKeyDict
from model_tools.activations import PytorchWrapper, KerasWrapper
from model_tools.activations.tensorflow import TensorflowSlimWrapper


def pytorch_model(function, image_size):
    module = import_module(f'torchvision.models')
    model_ctr = getattr(module, function)
    from model_tools.activations.pytorch import load_preprocess_images
    preprocessing = functools.partial(load_preprocess_images, image_size=image_size)
    return PytorchWrapper(model_ctr(pretrained=True), preprocessing)


def keras_model(module, model_function, image_size):
    module = import_module(f"keras.applications.{module}")
    model_ctr, model_preprocessing = getattr(module, model_function), getattr(module, "preprocess_input")
    from model_tools.activations.keras import load_images
    load_preprocess = lambda image_filepaths: model_preprocessing(load_images(image_filepaths, image_size=image_size))
    return KerasWrapper(model_ctr(), load_preprocess)


class TFSlimModel:
    @staticmethod
    def init(identifier, preprocessing_type, image_size, net_name=None, labels_offset=1, batch_size=64):
        import tensorflow as tf
        from nets import nets_factory

        placeholder = tf.placeholder(dtype=tf.string, shape=[batch_size])
        preprocess = TFSlimModel._init_preprocessing(placeholder, preprocessing_type, image_size=image_size)

        net_name = net_name or identifier
        model_ctr = nets_factory.get_network_fn(net_name, num_classes=labels_offset + 1000, is_training=False)
        logits, endpoints = model_ctr(preprocess)

        session = tf.Session()
        TFSlimModel._restore_imagenet_weights(identifier, session)
        return TensorflowSlimWrapper(identifier=identifier, endpoints=endpoints, logits=logits, inputs=placeholder,
                                     session=session, batch_size=batch_size, labels_offset=labels_offset)

    @staticmethod
    def _init_preprocessing(placeholder, preprocessing_type, image_size):
        import tensorflow as tf
        from preprocessing import vgg_preprocessing, inception_preprocessing
        from model_tools.activations.tensorflow import load_resize_image
        preprocessing_types = {
            'vgg': lambda image: vgg_preprocessing.preprocess_image(
                image, image_size, image_size, resize_side_min=image_size),
            'inception': lambda image: inception_preprocessing.preprocess_for_eval(
                image, image_size, image_size, central_fraction=1.)
        }
        assert preprocessing_type in preprocessing_types
        preprocess_image = preprocessing_types[preprocessing_type]
        preprocess = lambda image_path: preprocess_image(load_resize_image(image_path, image_size))
        preprocess = tf.map_fn(preprocess, placeholder, dtype=tf.float32)
        return preprocess

    @staticmethod
    def _restore_imagenet_weights(name, session):
        import tensorflow as tf
        var_list = None
        if name.startswith('mobilenet'):
            # Restore using exponential moving average since it produces (1.5-2%) higher accuracy according to
            # https://github.com/tensorflow/models/blob/a6494752575fad4d95e92698dbfb88eb086d8526/research/slim/nets/mobilenet/mobilenet_example.ipynb
            ema = tf.train.ExponentialMovingAverage(0.999)
            var_list = ema.variables_to_restore()
        restorer = tf.train.Saver(var_list)

        restore_path = TFSlimModel._find_model_weights(name)
        restorer.restore(session, restore_path)

    @staticmethod
    def _find_model_weights(model_name):
        _logger = logging.getLogger(fullname(TFSlimModel._find_model_weights))
        framework_home = os.path.expanduser(os.getenv('CM_HOME', '~/.candidate_models'))
        weights_path = os.getenv('CM_TSLIM_WEIGHTS_DIR', os.path.join(framework_home, 'model-weights', 'slim'))
        model_path = os.path.join(weights_path, model_name)
        if not os.path.isdir(model_path):
            _logger.debug(f"Downloading weights for {model_name} to {model_path}")
            os.makedirs(model_path)
            s3.download_folder(f"slim/{model_name}", model_path)
        fnames = glob.glob(os.path.join(model_path, '*.ckpt*'))
        assert len(fnames) > 0
        restore_path = fnames[0].split('.ckpt')[0] + '.ckpt'
        return restore_path


base_model_pool = UniqueKeyDict()
"""
Provides a set of standard models.
Each entry maps from `name` to an activations extractor.
"""

_key_functions = {
    'alexnet': lambda: pytorch_model('alexnet', image_size=224),
    'squeezenet1_0': lambda: pytorch_model('squeezenet1_0', image_size=224),
    'squeezenet1_1': lambda: pytorch_model('squeezenet1_1', image_size=224),
    'resnet-18': lambda: pytorch_model('resnet18', image_size=224),
    'resnet-34': lambda: pytorch_model('resnet34', image_size=224),

    'vgg-16': lambda: keras_model('vgg16', 'VGG16', image_size=224),
    'vgg-19': lambda: keras_model('vgg19', 'VGG19', image_size=224),
    'xception': lambda: keras_model('xception', 'Xception', image_size=299),
    'densenet-121': lambda: keras_model('densenet', 'DenseNet121', image_size=224),
    'densenet-169': lambda: keras_model('densenet', 'DenseNet169', image_size=224),
    'densenet-201': lambda: keras_model('densenet', 'DenseNet201', image_size=224),

    'inception_v1': lambda: TFSlimModel.init('inception_v1', preprocessing_type='inception', image_size=224),
    'inception_v2': lambda: TFSlimModel.init('inception_v2', preprocessing_type='inception', image_size=224),
    'inception_v3': lambda: TFSlimModel.init('inception_v3', preprocessing_type='inception', image_size=299),
    'inception_v4': lambda: TFSlimModel.init('inception_v4', preprocessing_type='inception', image_size=299),
    'inception_resnet_v2': lambda: TFSlimModel.init('inception_resnet_v2', preprocessing_type='inception',
                                                    image_size=299),
    'resnet-50_v1': lambda: TFSlimModel.init('resnet_v1_50', preprocessing_type='vgg',
                                             image_size=224, labels_offset=0),
    'resnet-101_v1': lambda: TFSlimModel.init('resnet_v1_101', preprocessing_type='vgg',
                                              image_size=224, labels_offset=0),
    'resnet-152_v1': lambda: TFSlimModel.init('resnet_v1_152', preprocessing_type='vgg',
                                              image_size=224, labels_offset=0),
    'resnet-50_v2': lambda: TFSlimModel.init('resnet_v2_50', preprocessing_type='inception', image_size=299),
    'resnet-101_v2': lambda: TFSlimModel.init('resnet_v2_101', preprocessing_type='inception', image_size=299),
    'resnet-152_v2': lambda: TFSlimModel.init('resnet_v2_152', preprocessing_type='inception', image_size=299),
    'nasnet_mobile': lambda: TFSlimModel.init('nasnet_mobile', preprocessing_type='inception', image_size=331),
    'nasnet_large': lambda: TFSlimModel.init('nasnet_large', preprocessing_type='inception', image_size=331),
    'pnasnet_large': lambda: TFSlimModel.init('pnasnet_large', preprocessing_type='inception', image_size=331),
}
for version, multiplier, image_size in [
    # v1
    (1, 1.0, 224), (1, 1.0, 192), (1, 1.0, 160), (1, 1.0, 128),
    (1, 0.75, 224), (1, 0.75, 192), (1, 0.75, 160), (1, 0.75, 128),
    (1, 0.5, 224), (1, 0.5, 192), (1, 0.5, 160), (1, 0.5, 128),
    (1, 0.25, 224), (1, 0.25, 192), (1, 0.25, 160), (1, 0.25, 128),
    # v2
    (2, 1.4, 224),
    (2, 1.3, 224),
    (2, 1.0, 224), (2, 1.0, 192), (2, 1.0, 160), (2, 1.0, 128), (2, 1.0, 96),
    (2, 0.75, 224), (2, 0.75, 192), (2, 0.75, 160), (2, 0.75, 128), (2, 0.75, 96),
    (2, 0.5, 224), (2, 0.5, 192), (2, 0.5, 160), (2, 0.5, 128), (2, 0.5, 96),
    (2, 0.35, 224), (2, 0.35, 192), (2, 0.35, 160), (2, 0.35, 128), (2, 0.35, 96),
]:
    identifier = f"mobilenet_v{version}_{multiplier}_{image_size}"
    if (version == 1 and multiplier in [.75, .5, .25]) or (version == 2 and multiplier == 1.4):
        net_name = f"mobilenet_v{version}_{multiplier * 100:03}"
    else:
        net_name = f"mobilenet_v{version}"
    _key_functions[identifier] = lambda: TFSlimModel.init(
        identifier, preprocessing_type='inception', image_size=image_size, net_name=net_name)
for key, function in _key_functions.items():
    # function=function default value enforces closure:
    # https://docs.python.org/3/faq/programming.html#why-do-lambdas-defined-in-a-loop-with-different-values-all-return-the-same-result
    base_model_pool[key] = LazyLoad(lambda function=function: function())