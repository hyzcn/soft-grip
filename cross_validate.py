# Author: Michał Bednarek PUT Poznan
# Comment: RNN + FC network implemented in Tensorflow for regression of stiffness of the gripped object.

import numpy as np
from argparse import ArgumentParser
import tensorflow as tf
import pickle, os

from sklearn.model_selection import KFold
from tqdm import tqdm
from net import RNN

config = tf.ConfigProto()
config.gpu_options.allow_growth = True
tf.enable_eager_execution(config)
tf.keras.backend.set_session(tf.Session(config=config))
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def create_tf_generators(train_dataset, train_idx, val_idx):
    train_x = train_dataset["data"][train_idx]
    train_y = train_dataset["stiffness"][train_idx]
    val_x = train_dataset["data"][val_idx]
    val_y = train_dataset["stiffness"][val_idx]
    train_ds = tf.data.Dataset.from_tensor_slices((train_x, train_y)) \
        .shuffle(50) \
        .batch(args.batch_size)
    val_ds = tf.data.Dataset.from_tensor_slices((val_x, val_y)) \
        .shuffle(50) \
        .batch(args.batch_size)
    return train_ds, val_ds


def validate(model, writer, ds, mean, std, abs_name, previous_steps):
    writer.set_as_default()

    mean_abs, mean_rms, stddev_abs, stddev_rms, mean_proc_abs, stddev_proc_abs = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    num_elements = 0
    for x_val, y_val in ds:
        x_val = tf.cast(x_val, tf.float32)
        y_val = tf.cast(y_val, tf.float32)

        predictions = model((x_val - mean) / std, training=False)
        rms = tf.losses.mean_squared_error(y_val, predictions, reduction=tf.losses.Reduction.NONE)

        # gather stats
        absolute = tf.sqrt(rms)
        mean_abs += tf.reduce_mean(absolute)
        mean_rms += tf.reduce_mean(rms)
        mean_proc_abs += tf.abs(tf.reduce_mean(y_val / predictions))
        stddev_abs += tf.math.reduce_std(absolute)
        stddev_rms += tf.math.reduce_std(rms)
        stddev_proc_abs += tf.abs(tf.math.reduce_std(y_val / predictions))
        num_elements += 1

    valabs = mean_abs / num_elements
    with tf.contrib.summary.always_record_summaries():
        tf.contrib.summary.scalar(abs_name, valabs, step=previous_steps)
        writer.flush()
    previous_steps += 1

    return previous_steps, {
        "mean_abs:": mean_abs / num_elements,
        "mean_rms:": mean_rms / num_elements,
        "mean_proc_abs:": mean_proc_abs / num_elements,
        "stddev_abs:": stddev_abs / num_elements,
        "stddev_rms:": stddev_rms / num_elements,
        "stddev_proc_abs:": stddev_proc_abs / num_elements}


def train(model, writer, ds, mean, std, optimizer, abs_name, previous_steps):
    writer.set_as_default()
    mean_abs, mean_rms, stddev_abs, stddev_rms, mean_proc_abs, stddev_proc_abs = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    num_elements = 0
    for x_train, y_train in ds:
        x_train = tf.cast(x_train, tf.float32)
        y_train = tf.cast(y_train, tf.float32)

        with tf.GradientTape() as tape:
            predictions = model((x_train - mean) / std, training=True)
            rms = tf.losses.mean_squared_error(y_train, predictions, reduction=tf.losses.Reduction.NONE)

            # gather stats
            absolute = tf.sqrt(rms)
            mean_abs += tf.reduce_mean(absolute)
            mean_rms += tf.reduce_mean(rms)
            mean_proc_abs += tf.abs(tf.reduce_mean(y_train / predictions))
            stddev_abs += tf.math.reduce_std(absolute)
            stddev_rms += tf.math.reduce_std(rms)
            stddev_proc_abs += tf.abs(tf.math.reduce_std(y_train / predictions))
            num_elements += 1

        gradients = tape.gradient(rms, model.trainable_variables)
        optimizer.apply_gradients(zip(gradients, model.trainable_variables))
        trainabs = mean_abs / num_elements
        with tf.contrib.summary.always_record_summaries():
            tf.contrib.summary.scalar(abs_name, trainabs, step=previous_steps)
            writer.flush()
        previous_steps += 1

    return previous_steps, {
        "mean_abs:": mean_abs / num_elements,
        "mean_rms:": mean_rms / num_elements,
        "mean_proc_abs:": mean_proc_abs / num_elements,
        "stddev_abs:": stddev_abs / num_elements,
        "stddev_rms:": stddev_rms / num_elements,
        "stddev_proc_abs:": stddev_proc_abs / num_elements}


def do_regression(args):
    with open(args.data_path_train, "rb") as fp:
        train_dataset = pickle.load(fp)
        train_mean = np.mean(train_dataset["data"], axis=(0, 1), keepdims=True)
        train_std = np.std(train_dataset["data"], axis=(0, 1), keepdims=True)

    unseen = None
    if args.data_path_unseen is not None:
        with open(args.data_path_unseen, "rb") as fp:
            unseen = pickle.load(fp)

    # setup model
    model = RNN(args.batch_size)

    # add eta decay
    eta = tf.contrib.eager.Variable(1e-3)
    eta_value = tf.train.exponential_decay(
        1e-3,
        tf.train.get_or_create_global_step(),
        args.epochs,
        0.99)
    eta.assign(eta_value())

    # setup optimizer and metrics
    optimizer = tf.keras.optimizers.Adam(eta)
    ckpt = tf.train.Checkpoint(optimizer=optimizer,
                               model=model,
                               optimizer_step=tf.train.get_or_create_global_step())

    # start a cross validate training
    kf = KFold(n_splits=args.num_splits)
    train_dataset["data"] = np.asarray(
        train_dataset["data"])  # needed as an array, because train_idx, val_idx are returned this way
    train_dataset["stiffness"] = np.asarray(train_dataset["stiffness"])
    os.makedirs(args.results, exist_ok=True)
    for split_no, (train_idx, val_idx) in enumerate(kf.split(train_dataset["data"], train_dataset["stiffness"])):
        # create pickle to dump metrics from each split
        logs_path = os.path.join(args.results, '{}'.format(split_no))
        pickle_path = os.path.join(args.results, 'metrics_{}.pickle'.format(split_no, split_no))
        metrics = open(pickle_path, 'wb')

        # create tensorflow writers and checkpoint saver
        ckpt_man = tf.train.CheckpointManager(ckpt, logs_path, max_to_keep=3)
        os.makedirs(logs_path, exist_ok=True)
        train_writer, val_writer, unseen_writer = tf.contrib.summary.create_file_writer(
            logs_path), tf.contrib.summary.create_file_writer(logs_path), tf.contrib.summary.create_file_writer(
            logs_path)

        # create split datasets to tf generators
        train_ds, val_ds = create_tf_generators(train_dataset, train_idx, val_idx)
        unseen_ds = None
        if unseen is not None:
            unseen_ds = tf.data.Dataset.from_tensor_slices((unseen["data"], unseen["stiffness"])) \
                .batch(args.batch_size)

        # start training // add metrics to tensorboard and save results for postprocessing
        plot_name_training_abs = 'train_{}/abs'.format(split_no)
        plot_name_validate_abs = 'val_{}/abs'.format(split_no)
        plot_name_unseen_abs = 'unseen_{}/abs'.format(split_no)
        train_step, val_step, unseen_step = 0, 0, 0
        train_results, val_results, unseen_results = list(), list(), list()
        for epoch in tqdm(range(args.epochs)):
            train_step, train_metrics = train(model, train_writer, train_ds, train_mean, train_std,
                                              optimizer, plot_name_training_abs, train_step)
            val_step, val_metrics = validate(model, val_writer, val_ds, train_mean, train_std,
                                             plot_name_validate_abs, val_step)

            # check the unseen dataset if needed and save to list
            train_results.append(train_metrics)
            val_results.append(val_metrics)
            if unseen is not None and unseen_ds is not None:
                unseen_step, unseen_metrics = validate(model, unseen_writer, unseen_ds, train_mean, train_std,
                                                       plot_name_unseen_abs, unseen_step)
                unseen_results.append(unseen_metrics)

            # assign eta and reset the metrics for the next epoch
            eta.assign(eta_value())

            # save each 10 epochs
            if epoch % 10 == 0:
                ckpt_man.save()

        # dump data
        obj = {
            "train_results": train_results,
            "val_results": val_results
        }
        if unseen_ds is not None:
            obj['unseen_results'] = unseen_results
        pickle.dump(obj, metrics)
        metrics.close()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--data-path-train', type=str,
                        default="./data/dataset/ds_IMU_no_contact_sense_full/train_dataset.pickle")
    parser.add_argument('--data-path-test', type=str,
                        default="./data/dataset/ds_IMU_no_contact_sense_full/test_dataset.pickle")
    parser.add_argument('--data-path-unseen', type=str,
                        default="./data/dataset/ds_IMU_no_contact_sense_full/unseen.pickle")
    parser.add_argument('--results', type=str, default="./data/logs/cross_validated")
    parser.add_argument('--epochs', type=int, default=250)
    parser.add_argument('--batch-size', type=int, default=256)
    parser.add_argument('--num-splits', type=int, default=5)
    args, _ = parser.parse_known_args()
    do_regression(args)
