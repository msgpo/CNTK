﻿# Copyright (c) Microsoft. All rights reserved.

# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

# NOTE:
# This example is meant as an illustration of how to use CNTKs distributed training feature from the python API.
# The training hyper parameters here are not necessarily optimal and for optimal convergence need to be tuned 
# for specific parallelization degrees that you want to run the example with.

import numpy as np
import sys
import os
from cntk import Trainer, distributed, device, persist
from cntk.learner import momentum_sgd, learning_rate_schedule
from cntk.ops import input_variable, constant, parameter, cross_entropy_with_softmax, combine, classification_error, times, element_times, pooling, AVG_POOLING, relu
from cntk.io import ReaderConfig, ImageDeserializer
from cntk.initializer import he_normal, glorot_uniform
from examples.CifarResNet.CifarResNet import create_reader, create_resnet_model

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(abs_path, "..", ".."))
from examples.common.nn import conv_bn_relu_layer, conv_bn_layer, linear_layer, print_training_progress

TRAIN_MAP_FILENAME = 'train_map.txt'
MEAN_FILENAME = 'CIFAR-10_mean.xml'
TEST_MAP_FILENAME = 'test_map.txt'

# Trains a residual network model on the Cifar image dataset
def cifar_resnet(data_path, run_test, num_epochs, communicator=None, save_model_filename=None, load_model_filename=None, debug_output=False):
    image_height = 32
    image_width = 32
    num_channels = 3
    num_classes = 10

    feats_stream_name = 'features'
    labels_stream_name = 'labels'

    minibatch_source = create_reader(os.path.join(data_path, 'train_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), True,
                                     distributed_communicator = communicator)

    features_si = minibatch_source[feats_stream_name]
    labels_si = minibatch_source[labels_stream_name]

    # Instantiate the resnet classification model, or load from file
    
    if load_model_filename:
        print("Loading model:", load_model_filename)
        classifier_output = persist.load_model(load_model_filename)
        image_input = classifier_output.arguments[0]
    else:
        image_input = input_variable(
            (num_channels, image_height, image_width), features_si.m_element_type)
        classifier_output = create_resnet_model(image_input, num_classes)

    # Input variables denoting the features and label data
    label_var = input_variable((num_classes), features_si.m_element_type)

    ce = cross_entropy_with_softmax(classifier_output, label_var)
    pe = classification_error(classifier_output, label_var)

    # Instantiate the trainer object to drive the model training

    mb_size = 128
    num_mb_per_epoch = 100
    
    num_mbs = num_mb_per_epoch * num_epochs

    lr_per_sample = [1/mb_size]*80+[0.1/mb_size]*40+[0.01/mb_size]
    lr_schedule = learning_rate_schedule(lr_per_sample, units = mb_size * num_mb_per_epoch)
    momentum_time_constant = -mb_size/np.log(0.9)

    # create data parallel distributed trainer if needed
    dist_trainer = distributed.data_parallel_distributed_trainer(communicator, False) if communicator else None

    # Instantiate the trainer object to drive the model training
    trainer = Trainer(classifier_output, ce, pe,
                      [momentum_sgd(classifier_output.parameters, lr_schedule, momentum_time_constant, l2_regularization_weight=0.0001)],
                      distributed_trainer = dist_trainer)
    
    # Get minibatches of images to train with and perform model training
    training_progress_output_freq = 100 if communicator else 20

    if debug_output:
        training_progress_output_freq = training_progress_output_freq/4
        
    for i in range(0, num_mbs):
        mb = minibatch_source.next_minibatch(mb_size)

        # Specify the mapping of input variables in the model to actual
        # minibatch data to be trained with
        arguments = {
                image_input: mb[features_si], 
                label_var: mb[labels_si]
                }
        trainer.train_minibatch(arguments)

        print_training_progress(trainer, i, training_progress_output_freq)
        
    if save_model_filename:
        print("Saving model:", save_model_filename)
        persist.save_model(classifier_output, save_model_filename)

    if run_test:
        test_minibatch_source = create_reader(os.path.join(data_path, 'test_map.txt'), os.path.join(data_path, 'CIFAR-10_mean.xml'), False)
        features_si = test_minibatch_source[feats_stream_name]
        labels_si = test_minibatch_source[labels_stream_name]

        mb_size = 128
        num_mbs = 100

        total_error = 0.0
        for i in range(0, num_mbs):
            mb = test_minibatch_source.next_minibatch(mb_size)

            # Specify the mapping of input variables in the model to actual
            # minibatch data to be trained with
            arguments = {
                    image_input: mb[features_si], 
                    label_var: mb[labels_si]
                    }
            error = trainer.test_minibatch(arguments)
            total_error += error

        return total_error / num_mbs
    else:
        return 0

if __name__ == '__main__':
    data_path = os.path.abspath(os.path.normpath(os.path.join(
        *"../../../../Examples/Image/DataSets/CIFAR-10/".split("/"))))

    os.chdir(data_path)

    # Create distributed communicator for 1-bit SGD
    communicator = distributed.communicator(distributed.quantized_mpi_communicator(1))
    workers = communicator.workers()
    current_worker = communicator.current_worker()
    print("List all distributed workers")
    for wk in workers:
        if current_worker.global_rank == wk.global_rank:
            print("* {} {}".format(wk.global_rank, wk.host_id))
        else:
            print("  {} {}".format(wk.global_rank, wk.host_id))

    print("Training on device type:{} id:{}".format('gpu' if device.default().type() else 'cpu', device.default().id()))

    start_model = "start_model.bin"
    num_start_epochs = 1
    num_parallel_epochs = 10

    # training the start model only in one worker
    if communicator.current_worker().global_rank == 0:
        cifar_resnet(data_path, save_model_filename=start_model, communicator=None, run_test=False, num_epochs=num_start_epochs)
    
    communicator.barrier()
    
    # train in parallel
    error = cifar_resnet(data_path, load_model_filename=start_model, communicator=communicator, run_test=True, num_epochs=num_parallel_epochs)
    
    print("Error: %f" % error)

    distributed.communicator.finalize()