# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Example script to train the DNC on a repeated copy task."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import sonnet as snt

import dnc
import arithmetic
import repeat_copy
import variable_assignment

FLAGS = tf.flags.FLAGS

# Model parameters
tf.flags.DEFINE_integer("hidden_size", 64, "Size of LSTM hidden layer.")
tf.flags.DEFINE_integer("depth", 1, "Depth of RNN controller.")
tf.flags.DEFINE_integer("memory_size", 16, "The number of memory slots.")
tf.flags.DEFINE_integer("word_size", 16, "The width of each memory slot.")
tf.flags.DEFINE_integer("num_write_heads", 1, "Number of memory write heads.")
tf.flags.DEFINE_integer("num_read_heads", 4, "Number of memory read heads.")
tf.flags.DEFINE_integer("clip_value", 20,
                        "Maximum absolute value of controller and dnc outputs.")
tf.flags.DEFINE_string("controller_type", "lstm",
                       "Which RNN to use as the controller")
tf.flags.DEFINE_bool("use_dnc", True,
                     "Whether to use the DNC or the raw controller rnn")

# Optimizer parameters.
tf.flags.DEFINE_float("max_grad_norm", 50, "Gradient clipping norm limit.")
tf.flags.DEFINE_float("learning_rate", 1e-4, "Optimizer learning rate.")
tf.flags.DEFINE_float("optimizer_epsilon", 1e-10,
                      "Epsilon used for RMSProp optimizer.")

# Task parameters
tf.flags.DEFINE_string("task", "repeat_copy",
                       "`repeat_copy` or `variable_assignment`: "
                       "the task we are training on.")
tf.flags.DEFINE_integer("batch_size", 16, "Batch size for training.")
tf.flags.DEFINE_integer("num_bits", 4, "Dimensionality of each vector to copy")
tf.flags.DEFINE_integer(
    "min_length", 1,
    "Lower limit on number of vectors in the observation pattern to copy")
tf.flags.DEFINE_integer(
    "max_length", 2,
    "Upper limit on number of vectors in the observation pattern to copy")
tf.flags.DEFINE_integer("min_repeats", 1,
                        "Lower limit on number of copy repeats.")
tf.flags.DEFINE_integer("max_repeats", 2,
                        "Upper limit on number of copy repeats.")

# Training options.
tf.flags.DEFINE_integer("num_training_iterations", 100000,
                        "Number of iterations to train for.")
tf.flags.DEFINE_integer("report_interval", 100,
                        "Iterations between reports (samples, valid loss).")
tf.flags.DEFINE_string("checkpoint_dir", "/tmp/tf/dnc",
                       "Checkpointing directory.")
tf.flags.DEFINE_integer("checkpoint_interval", -1,
                        "Checkpointing step interval.")
tf.flags.DEFINE_integer("summary_interval", -1,
                        "Summary step interval.")
tf.flags.DEFINE_float('stop_threshold', 1.0, 'threshold for early stopping')


def run_model(input_sequence, output_size):
  """Runs model on input sequence."""

  if input_sequence.dtype == tf.int32:
    input_sequence = tf.one_hot(input_sequence,
                                output_size,
                                dtype=tf.float32)

  access_config = {
      "memory_size": FLAGS.memory_size,
      "word_size": FLAGS.word_size,
      "num_reads": FLAGS.num_read_heads,
      "num_writes": FLAGS.num_write_heads,
  }

  controller_config = {
      "hidden_size": FLAGS.hidden_size,
      "depth": FLAGS.depth,
      "cell_type": FLAGS.controller_type
  }

  clip_value = FLAGS.clip_value

  if FLAGS.use_dnc:
    dnc_core = dnc.DNC(access_config,
                       controller_config,
                       output_size,
                       clip_value)
  else:
    dnc_core = dnc.get_controller(**controller_config)
  initial_state = dnc_core.initial_state(FLAGS.batch_size)
  output_sequence, _ = tf.nn.dynamic_rnn(
      cell=dnc_core,
      inputs=input_sequence,
      time_major=True,
      initial_state=initial_state)

  # NB
  if output_sequence.get_shape()[-1] != output_size:
    final_projection = snt.BatchApply(snt.Linear(output_size))
    output_sequence = final_projection(output_sequence)

  return output_sequence


def train(num_training_iterations, report_interval):
  """Trains the DNC and periodically reports the loss."""

  if FLAGS.task == "repeat_copy":
    dataset = repeat_copy.RepeatCopy(FLAGS.num_bits, FLAGS.batch_size,
                                     FLAGS.min_length, FLAGS.max_length,
                                     FLAGS.min_repeats, FLAGS.max_repeats)
  elif FLAGS.task == "variable_assignment":
    dataset = variable_assignment.VariableAssignment(FLAGS.batch_size,
                                                     log_prob_in_bits=True)
  elif FLAGS.task == "addition":
    dataset = arithmetic.Addition(FLAGS.batch_size)
  else:
    raise ValueError("Unknown task: {}".format(FLAGS.task))

  dataset_tensors = dataset()

  output_logits = run_model(dataset_tensors.observations, dataset.target_size)
  # Used for visualization.
  output = tf.round(
      tf.expand_dims(dataset_tensors.mask, -1) * tf.sigmoid(output_logits))

  train_loss = dataset.cost(output_logits, dataset_tensors.target,
                            dataset_tensors.mask)
  tf.summary.scalar('train_loss', train_loss)

  # Set up optimizer with global norm clipping.
  trainable_variables = tf.trainable_variables()
  grads, _ = tf.clip_by_global_norm(
      tf.gradients(train_loss, trainable_variables), FLAGS.max_grad_norm)

  global_step = tf.get_variable(
      name="global_step",
      shape=[],
      dtype=tf.int64,
      initializer=tf.zeros_initializer(),
      trainable=False,
      collections=[tf.GraphKeys.GLOBAL_VARIABLES, tf.GraphKeys.GLOBAL_STEP])
  lr = tf.train.exponential_decay(FLAGS.learning_rate, global_step,
                                  decay_steps=10000, decay_rate=0.9)
  tf.summary.scalar('learning_rate', lr)
  optimizer = tf.train.RMSPropOptimizer(
      lr, epsilon=FLAGS.optimizer_epsilon)
  train_step = optimizer.apply_gradients(
      zip(grads, trainable_variables), global_step=global_step)

  saver = tf.train.Saver()

  if FLAGS.checkpoint_interval > 0:
    hooks = [
        tf.train.CheckpointSaverHook(
            checkpoint_dir=FLAGS.checkpoint_dir,
            save_steps=FLAGS.checkpoint_interval,
            saver=saver)
    ]
  else:
    hooks = []
  if FLAGS.summary_interval > 0:
    hooks.append(tf.train.SummarySaverHook(
        save_steps=FLAGS.summary_interval,
        output_dir=FLAGS.checkpoint_dir,
        summary_op=tf.summary.merge_all()))

  # Train.
  with tf.train.SingularMonitoredSession(
      hooks=hooks, checkpoint_dir=FLAGS.checkpoint_dir) as sess:

    start_iteration = sess.run(global_step)
    total_loss = 0

    for train_iteration in xrange(start_iteration, num_training_iterations):
      _, loss = sess.run([train_step, train_loss])
      total_loss += loss

      if (train_iteration + 1) % report_interval == 0:
        dataset_tensors_np, output_np = sess.run([dataset_tensors, output])
        dataset_string = dataset.to_human_readable(dataset_tensors_np,
                                                   output_np)
        tf.logging.info("%d: Avg training loss %f.\n%s",
                        train_iteration, total_loss / report_interval,
                        dataset_string)
        if (total_loss / report_interval) <= FLAGS.stop_threshold:
          # got it
          tf.logging.info('Training loss below %f, exiting early.',
                          FLAGS.stop_threshold)
          break
        total_loss = 0
  # monitored session should clean up after itself?
  tf.logging.info("Finished")

def main(unused_argv):
  tf.logging.set_verbosity(3)  # Print INFO log messages.
  train(FLAGS.num_training_iterations, FLAGS.report_interval)


if __name__ == "__main__":
  tf.app.run()
