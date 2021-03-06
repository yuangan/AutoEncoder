//
//  AutoEncoder.py
//  python
//
//  
//
import tensorflow as tf
import tflearn
import numpy as np
import sys
import os
import argparse
import time
import scipy.io as sio
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
print ROOT_DIR
sample_dir = os.path.join(ROOT_DIR, 'external')
print sample_dir
sys.path.append(sample_dir)

from sampling import farthest_point_sample, gather_point
from structural_losses import nn_distance, approx_match, match_cost
from tf_utils import expand_scope_by_name, replicate_parameter_for_all_layers

ALL_SIZE = 6400
NUM_FILE = 64

def encoder(pc_inp, n_pc_points=10240, n_filters=[64, 128, 256, 1024], filter_sizes=[1, 1, 1, 1], strides=[1, 1, 1, 1], weight_decay=0.001,
            b_norm=True, regularizer=None, non_linearity=tf.nn.leaky_relu, symmetry=tf.reduce_max,
            dropout_prob=None, pool_sizes=None, scope=None, reuse=False,
            padding='same', verbose=False, closing=None):
    n_layers = len(n_filters)
    assert n_layers == len(filter_sizes)
    assert n_layers == len(strides)
    #'n_filters': [64, 128, 128, 256, 128]
    x = pc_inp
    for i in xrange(n_layers):
        # conv
        name = "encoder_conv_layer_" + str(i)
        scope_i = expand_scope_by_name(scope, name)
        x = tflearn.layers.conv.conv_1d(x, nb_filter=n_filters[i], filter_size=filter_sizes[i], strides=strides[i],
                                        regularizer=regularizer, weight_decay=weight_decay, name=name, reuse=reuse, scope=scope_i, padding=padding)
        if verbose:
            print(name, 'conv params = ', np.prod(
                x.W.get_shape().as_list()) + np.prod(x.b.get_shape().as_list()))
        # bn
        if b_norm:
            name += '_bnorm'
            scope_i = expand_scope_by_name(scope, name)
            x = tflearn.layers.normalization.batch_normalization(
                x, name=name, reuse=reuse, scope=scope_i)
            if verbose:
                print('bnorm params = ', np.prod(x.beta.get_shape(
                ).as_list()) + np.prod(x.gamma.get_shape().as_list()))
        # non_linearity
        if non_linearity is not None:
            x = non_linearity(x)
    if symmetry is not None:
        x = symmetry(x, axis=1)
        if verbose:
            print(x)
    return x


def decoder(z_inp, layer_sizes=[256, 256, 6144], b_norm=True, non_linearity=tf.nn.relu,
            regularizer=None, weight_decay=0.001, reuse=False, scope=None, dropout_prob=None,
            b_norm_finish=False, verbose=False):
    if verbose:
        print('Building Decoder')
    layer = z_inp
    n_layers = len(layer_sizes)
    for i, filter_size in enumerate(layer_sizes):
        name = 'decoder_fc_' + str(i)
        scope_i = expand_scope_by_name(scope, name)
        layer = tflearn.layers.core.fully_connected(layer, filter_size, activation='linear', weights_init='xavier',
                                                    name=name, regularizer=regularizer, weight_decay=weight_decay, reuse=reuse, scope=scope_i)
        if verbose:
            print(name, 'FC params = ', np.prod(
                layer.W.get_shape().as_list()) + np.prod(layer.b.get_shape().as_list()))
        if i < (n_layers - 1):
            layer = non_linearity(layer)
    return layer


def build(resourceid=0, n_pc_points=10240, latent_size=128, loss_type='cd', reg_alpha=0.1, learning_rate=3e-5, decay_steps=30000, exponential_decay=False):
    encoder_args = {
        'n_pc_points': n_pc_points,
        'n_filters': [64, 128, 128, 256, 256, 512, 1024, 128],
        'filter_sizes': [1] * 8,
        'strides': [1] * 8,
        'b_norm': False,
        'verbose': True
    }

    decoder_args = {
        'layer_sizes': [256, 512, 1024, n_pc_points * 3],
        'b_norm': False,
        'b_norm_finish': False,
        'verbose': True
    }
    with tf.device('/gpu:%d' % resourceid):
        inp_pc = tf.placeholder(tf.float32, shape=[None, n_pc_points, 3])
        inp_sample = inp_pc
        with tf.variable_scope('discriminator_ae') as scope:
            inp = tflearn.layers.normalization.batch_normalization(inp_sample)
            z = encoder(inp, **encoder_args)
        with tf.variable_scope('generator_ae') as scope:
            out = decoder(z, **decoder_args)

        reconstr = tf.reshape(out, [-1, n_pc_points, 3])

        dists_forward, _, dists_backward, _ = nn_distance(
            reconstr, inp_sample)
        dists_forward = tf.reduce_mean(dists_forward)
        dists_backward = tf.reduce_mean(dists_backward)
        cd_loss = (dists_forward + dists_backward) / 2.0 * 100000.0

        match = approx_match(reconstr, inp_sample)
        emd_loss = tf.reduce_mean(match_cost(reconstr, inp_sample, match))

        lr = 2e-5
        global_step = tf.Variable(0, dtype=tf.int32, trainable=False)
        if exponential_decay:
            lr = tf.train.exponential_decay(learning_rate, global_step, decay_steps,
                                            decay_rate=0.5, staircase=True, name="learning_rate_decay")
            lr = tf.maximum(lr, 1e-5)
        optimizer_cd = tf.train.AdamOptimizer(learning_rate=lr)
        optimizer_emd = tf.train.AdamOptimizer(learning_rate=lr)
        optimizer_all = tf.train.AdamOptimizer(learning_rate=lr)
        train_cd_op = optimizer_cd.minimize(cd_loss, global_step=global_step)
        train_emd_op = optimizer_emd.minimize(
            emd_loss, global_step=global_step)
        train_all_op = optimizer_cd.minimize(cd_loss+emd_loss, global_step=global_step)
        batchnoinc = global_step.assign(global_step + 1)
    return inp_pc, inp_sample, cd_loss, emd_loss, z, reconstr, train_cd_op, train_emd_op, global_step, batchnoinc, train_all_op,dists_forward,dists_backward

def readoff(path, i):
    filepath = path[i]
    return np.loadtxt(filepath, skiprow=2)

def main(data_dir, resourceid, keyname, dumpdir):
    path = []
    for(root, file_dir, filenames) in os.walk(data_dir):
        for filename in filenames:
            path.append(root+'/'+filename)
    print path
    if not os.path.exists(dumpdir):
        os.system("mkdir -p %s" % dumpdir)
    inp_pc, inp_sample, cd_loss, emd_loss, z, reconstr, train_cd_op, train_emd_op, global_step, batchnoinc, train_all_op,dists_forward,dists_backward = build(
        resourceid, n_pc_points, bneck, loss_type, reg_alpha, learning_rate)
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    saver = tf.train.Saver()
    with tf.Session(config=config)as sess, open('%s/%s.log' % (dumpdir, keyname), 'a') as fout:
        sess.run(tf.global_variables_initializer())
        #saver.restore(sess,"%s_bak2/%s.ckpt"%(dumpdir,keyname))
        lastsave = time.time()
        bno = sess.run(global_step)  # bno
        t2 = time.time()
        trainloss_accs_cd = 0.0
        trainloss_acc0_cd = 1e-9
        trainloss_accs_emd = 0.0
        trainloss_acc0_emd = 1e-9
        validloss_accs_cd = 0.0
        validloss_acc0_cd = 1e-9
        validloss_accs_emd = 0.0
        validloss_acc0_emd = 1e-9

        emd_loss__0, cd_loss__0 = 0, 0
        #step < all_dataset
        while bno < ALL_SIZE:
            t1 = t2
            #todo: add inputs_pc to data
            validating = 0
            data = readoff(path, bno%NUM_FILE).reshape(1, 10240, 3)
            print data.shape
            if not validating:
                #_, emd_loss__0 = sess.run([train_all_op,emd_loss],feed_dict={inp_pc:data})
                #trainloss_accs_emd = trainloss_accs_emd*0.99+emd_loss__0
                #trainloss_acc0_emd = trainloss_acc0_emd*0.99+1
                #trainloss_accs_cd = trainloss_accs_cd*0.99+cd_loss__0
                #trainloss_acc0_cd = trainloss_acc0_cd*0.99+1
                if np.random.rand() < 0.25:
                    _, emd_loss__0 = sess.run(
                        [train_emd_op, emd_loss], feed_dict={inp_pc: data})
                    trainloss_accs_emd = trainloss_accs_emd*0.99+emd_loss__0
                    trainloss_acc0_emd = trainloss_acc0_emd*0.99+1
                else:
                    _, cd_loss__0 = sess.run(
                        [train_cd_op, cd_loss], feed_dict={inp_pc: data})
                    trainloss_accs_cd = trainloss_accs_cd*0.99+cd_loss__0
                    trainloss_acc0_cd = trainloss_acc0_cd*0.99+1
            elif validating == 1:
                _, emd_loss__0, cd_loss__0 = sess.run(
                    [batchnoinc, emd_loss, cd_loss], feed_dict={inp_pc: data})
                validloss_accs_emd = validloss_accs_emd*0.997+emd_loss__0
                validloss_acc0_emd = validloss_acc0_emd*0.997+1
                validloss_accs_cd = validloss_accs_cd*0.997+cd_loss__0
                validloss_acc0_cd = validloss_acc0_cd*0.997+1
            else:
                bno = sess.run(global_step)
                continue
            out_type = 'train' if validating == 0 else 'validate'
            t2 = time.time()
            fout.write("step %d %s: cd_loss: %g,emd_loss: %g, t_AVG_cd_loss: %g, t_AVA_emd_loss: %g,v_AVA_cd_loss: %g,v_AVG_emd_loss: %g, time: %g\n" % (
                bno, out_type, cd_loss__0, emd_loss__0, trainloss_accs_cd/trainloss_acc0_cd, trainloss_accs_emd/trainloss_acc0_emd, validloss_accs_cd/validloss_acc0_cd, validloss_accs_emd/validloss_acc0_emd, t2-t1))
            print("step %d %s: cd_loss: %g,emd_loss: %g, t_AVG_cd_loss: %g, t_AVA_emd_loss: %g,v_AVA_cd_loss: %g,v_AVG_emd_loss: %g, time: %g\n" % (
                bno, out_type, cd_loss__0, emd_loss__0, trainloss_accs_cd/trainloss_acc0_cd, trainloss_accs_emd/trainloss_acc0_emd, validloss_accs_cd/validloss_acc0_cd, validloss_accs_emd/validloss_acc0_emd, t2-t1))
            if bno % 128 == 0:
                fout.flush()
            if t2-lastsave > 1200:
                saver.save(sess, '%s/%s.ckpt' % (dumpdir, keyname))
                lastsave = t2
            bno = sess.run(global_step)
        saver.save(sess, '%s/%s.ckpt' % (dumpdir, keyname))

def prediction(data_dir, resourceid, keyname, dumpdir):
    inp_pc, inp_sample, cd_loss, emd_loss, z, reconstr, train_cd_op, train_emd_op, global_step, batchnoinc, train_all_op,dists_forward,dists_backward = build(
        resourceid, n_pc_points, bneck, loss_type, reg_alpha, learning_rate)
    dists_forward = tf.sqrt(dists_forward)
    dists_backward = tf.sqrt(dists_backward)
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    saver = tf.train.Saver()
    with tf.Session(config=config)as sess, open('%s/%s.log' % (dumpdir, keyname), 'a') as fout:
        #sess.run(tf.global_variables_initializer())
        saver.restore(sess,"%s_bak2/%s.ckpt"%(dumpdir,keyname))
        sum_fd=0
        sum_bd=0
        for i in xrange(ALL_SIZE-TV_SIZE):
            data, validating = fetch_batch()
            fd,bd = sess.run(
                        [dists_forward,dists_backward], feed_dict={inp_pc: data})
            sum_fd+=fd
            sum_bd+=bd
            print('%d fd:%g, bd:%g, fd_avg:%g, bd_avg:%g'%(i,fd,bd,sum_fd/(i+1),sum_bd/(i+1)))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    #input data to train
    parser.add_argument("-d", "--data_dir", help="path for input pc",
                        default="/media/iot/mydisk21/gy/data/train/Chinese/")
    parser.add_argument("-r", "--resourceid",
                        help="number of gpu for use", type=int, default=0)

    parser.add_argument(
        "-k", "--keyname", help="keyname you like", default="ae")
    parser.add_argument("-dm", "--dumpdir",
                        help="dumpdir you like", default="/media/iot/mydisk21/gy/data/AutoEncoder/")
    parser.add_argument("-pre", "--preddir",
                        help="preddir you like", default="./AE_z_128")
    parser.add_argument("-pref", "--predflag",
                        help="predflag you like", type=int, default=2)
    parser.add_argument("cmd", help="train or prediction", default="train")
    args = parser.parse_args()

    print 'dumpdir= '+args.dumpdir
    print 'data_dir= '+args.data_dir
    print 'resourceid= %d' % (args.resourceid)
    print 'keyname= '+args.keyname
    print 'pred_dir= '+args.preddir
    print 'predflag= %d' % args.predflag
    print 'cmd= '+args.cmd
    os.environ['TF_ENABLE_WINOGRAD_NONFUSED'] = '1'
    if args.cmd == 'train':
        main(arg.data_dir, args.resourceid, args.keyname, args.dumpdir)
    if args.cmd == 'prediction':
        prediction(arg.data_dir, args.resourceid, args.keyname, args.dumpdir)
