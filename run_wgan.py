from __future__ import print_function
import argparse
import random
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from torch.autograd import Variable
import os
import numpy as np
import copy

import wgan_models.dcgan as dcgan
import wgan_models.mlp as mlp
import support_lib
import config
import subprocess
import time
import gsa_io

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default='lsun', help='cifar10 | lsun | imagenet | folder | lfw ')
parser.add_argument('--dataroot', default='../../dataset', help='path to dataset')
parser.add_argument('--workers', type=int, help='number of data loading workers', default=16)
parser.add_argument('--batchSize', type=int, default=config.gsa_batchsize, help='input batch size')
parser.add_argument('--imageSize', type=int, default=config.gsa_size, help='the height / width of the input image to network')
parser.add_argument('--nc', type=int, default=3, help='input image channels')
parser.add_argument('--nz', type=int, default=100, help='size of the latent z vector')
parser.add_argument('--ngf', type=int, default=64)
parser.add_argument('--ndf', type=int, default=64)
parser.add_argument('--niter', type=int, default=25, help='number of epochs to train for')
parser.add_argument('--lrD', type=float, default=0.00005, help='learning rate for Critic, default=0.00005')
parser.add_argument('--lrG', type=float, default=0.00005, help='learning rate for Generator, default=0.00005')
parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')
parser.add_argument('--cuda'  , default=True, action='store_true', help='enables cuda')
parser.add_argument('--ngpu'  , type=int, default=1, help='number of GPUs to use')
parser.add_argument('--netG', default='', help="path to netG (to continue training)")
parser.add_argument('--netD', default='', help="path to netD (to continue training)")
parser.add_argument('--clamp_lower', type=float, default=-0.01)
parser.add_argument('--clamp_upper', type=float, default=0.01)
parser.add_argument('--Diters', type=int, default=5, help='number of D iters per each G iter')
parser.add_argument('--noBN', action='store_true', help='use batchnorm or not (only for DCGAN)')
parser.add_argument('--mlp_G', action='store_true', help='use MLP for G')
parser.add_argument('--mlp_D', action='store_true', help='use MLP for D')
parser.add_argument('--n_extra_layers', type=int, default=0, help='Number of extra layers on gen and disc')
parser.add_argument('--experiment', default=config.logdir, help='Where to store samples and models')
parser.add_argument('--adam', action='store_true', help='Whether to use adam (default is rmsprop)')
opt = parser.parse_args()
print(opt)

# Where to store samples and models
if opt.experiment is None:
    opt.experiment = 'samples'
os.system('mkdir {0}'.format(opt.experiment))

# random seed for
opt.manualSeed = random.randint(1, 10000) # fix seed
print("Random Seed: ", opt.manualSeed)
random.seed(opt.manualSeed)
torch.manual_seed(opt.manualSeed)

cudnn.benchmark = True

# load dataset
if torch.cuda.is_available() and not opt.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

if opt.dataset in ['imagenet', 'folder', 'lfw']:
    # folder dataset
    dataset = dset.ImageFolder(root=opt.dataroot,
                               transform=transforms.Compose([
                                   transforms.Scale(opt.imageSize),
                                   transforms.CenterCrop(opt.imageSize),
                                   transforms.ToTensor(),
                                   transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                               ]))
elif opt.dataset == 'lsun':
    dataset = dset.LSUN(db_path=opt.dataroot, classes=['bedroom_train'],
                        transform=transforms.Compose([
                            transforms.Scale(opt.imageSize),
                            transforms.CenterCrop(opt.imageSize),
                            transforms.ToTensor(),
                            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                        ]))
elif opt.dataset == 'cifar10':
    dataset = dset.CIFAR10(root=opt.dataroot, download=True,
                           transform=transforms.Compose([
                               transforms.Scale(opt.imageSize),
                               transforms.ToTensor(),
                               transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                           ])
    )
assert dataset
dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                         shuffle=True, num_workers=int(opt.workers))

ngpu = int(opt.ngpu)
nz = int(opt.nz)
ngf = int(opt.ngf)
ndf = int(opt.ndf)
nc = int(opt.nc)
n_extra_layers = int(opt.n_extra_layers)

# custom weights initialization called on netG and netD
def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

'''create netG'''
if opt.noBN:
    netG = dcgan.DCGAN_G_nobn(opt.imageSize, nz, nc, ngf, ngpu, n_extra_layers)
elif opt.mlp_G:
    netG = mlp.MLP_G(opt.imageSize, nz, nc, ngf, ngpu)
else:
    # in the paper. this is the best implementation
    netG = dcgan.DCGAN_G(opt.imageSize, nz, nc, ngf, ngpu, n_extra_layers)

netG.apply(weights_init)

# load checkpoint if needed
if opt.netG != '':
    netG.load_state_dict(torch.load(opt.netG))
print(netG)

if opt.mlp_D:
    netD = mlp.MLP_D(opt.imageSize, nz, nc, ndf, ngpu)
else:
    netD = dcgan.DCGAN_D(opt.imageSize, nz, nc, ndf, ngpu, n_extra_layers)
    netD.apply(weights_init)

# load checkpoint if needed
if opt.netD != '':
    netD.load_state_dict(torch.load(opt.netD))
print(netD)

input = torch.FloatTensor(opt.batchSize, 3, opt.imageSize, opt.imageSize)
noise = torch.FloatTensor(opt.batchSize, nz, 1, 1)
fixed_noise = torch.FloatTensor(opt.batchSize, nz, 1, 1).normal_(0, 1)
one = torch.FloatTensor([1])
mone = one * -1

if opt.cuda:
    netD.cuda()
    netG.cuda()
    input = input.cuda()
    one, mone = one.cuda(), mone.cuda()
    noise, fixed_noise = noise.cuda(), fixed_noise.cuda()

# setup optimizer
if opt.adam:
    optimizerD = optim.Adam(netD.parameters(), lr=opt.lrD, betas=(opt.beta1, 0.999))
    optimizerG = optim.Adam(netG.parameters(), lr=opt.lrG, betas=(opt.beta1, 0.999))
else:
    optimizerD = optim.RMSprop(netD.parameters(), lr = opt.lrD)
    optimizerG = optim.RMSprop(netG.parameters(), lr = opt.lrG)

real_cpu_recorder = []
real_cpu_recorder_path = []
gen_iterations = 0
for epoch in range(opt.niter):
    
    data_iter = iter(dataloader)
    i = 0
    while i < len(dataloader):

        ######################################################################
        ########################### Update D network #########################
        ######################################################################

        '''
            when train D network, paramters of D network in trained,
            reset requires_grad of D network to true.
            (they are set to False below in netG update)
        '''
        for p in netD.parameters():
            p.requires_grad = True

        '''
            train the discriminator Diters times
            Diters is set to 100 only on the first 25 generator iterations or
            very sporadically (once every 500 generator iterations).
            This helps to start with the critic at optimum even in the first iterations.
            There shouldn't be a major difference in performance, but it can help,
            especially when visualizing learning curves (since otherwise you'd see the
            loss going up until the critic is properly trained).
            This is also why the first 25 iterations take significantly longer than
            the rest of the training as well.
        '''
        if gen_iterations < 25 or gen_iterations % 500 == 0:
            Diters = 100
        else:
            Diters = opt.Diters

        '''
            start interation training of D network
            D network is trained for sevrel steps when 
            G network is trained for one time
        '''
        j = 0
        while j < Diters and i < len(dataloader):
            j += 1

            # clamp parameters to a cube
            for p in netD.parameters():
                p.data.clamp_(opt.clamp_lower, opt.clamp_upper)

            # next data
            data = data_iter.next()
            i += 1

            # train D network with real
            if config.enable_gsa:
                # # input gsa state
                # real_cpu = torch.FloatTensor(np.ones((64,3,64,64)))

                # load history
                real_cpu_loader = copy.deepcopy(real_cpu_recorder)
                real_cpu_loader_path = copy.deepcopy(real_cpu_recorder_path)
                # del history
                del real_cpu_recorder
                del real_cpu_recorder_path

                while True:

                    # load a batch
                    try:
                        path_dic_of_requiring_file=gsa_io.GetFileList(FindPath=config.real_state_dir,
                                                               FlagStr=['__requiring.npz'])
                    except Exception, e:
                        print('find file list fialed with error: '+str(Exception)+": "+str(e))
                        continue

                    for i in range(len(path_dic_of_requiring_file)):

                        path_of_requiring_file = path_dic_of_requiring_file[i]

                        if path_of_requiring_file.split('.np')[1] is not 'z':
                            print('find npz temp file: '+path_of_requiring_file+'>>pass')
                            continue

                        try:
                            requiring_state = np.load(path_of_requiring_file)['state']
                            real_cpu_loader += [requiring_state]
                            real_cpu_loader_path += [path_of_requiring_file]
                        except Exception, e:
                            print('load requiring_state error: '+str(Exception)+": "+str(e))
                            continue

                        subprocess.call(["mv", path_of_requiring_file, path_of_requiring_file.split('__')[0]+'__done.npz'])

                    # cut
                    if len(real_cpu_loader) >= opt.batchSize:

                        print('load real_cpu_loader: '+str(np.shape(real_cpu_loader)))

                        # load used part
                        real_cpu = copy.deepcopy(real_cpu_loader)[0:opt.batchSize]
                        print('load real_cpu: '+str(np.shape(real_cpu)))
                        real_cpu=np.asarray(real_cpu)
                        real_cpu = torch.FloatTensor(real_cpu)
                        real_cpu_path = copy.deepcopy(real_cpu_loader_path)[0:opt.batchSize]

                        # record uused part
                        real_cpu_recorder = copy.deepcopy(real_cpu_loader)[opt.batchSize:-1]
                        real_cpu_recorder_path = copy.deepcopy(real_cpu_loader_path)[opt.batchSize:-1]
                        print('load real_cpu_recorder: '+str(np.shape(real_cpu_recorder)))

                        # del loader
                        del real_cpu_loader
                        del real_cpu_loader_path

                        # break out loader loop
                        break
            else:
                # use data in dataloader
                real_cpu, _ = data

            

            netD.zero_grad()
            batch_size = real_cpu.size(0)

            if opt.cuda:
                real_cpu = real_cpu.cuda()

            input.resize_as_(real_cpu).copy_(real_cpu)
            inputv = Variable(input)

            errD_real, outputD_real = netD(inputv)
            print('real prob')
            print(errD_real)
            errD_real.backward(one)

            if config.enable_gsa:
                # # output the gsa reward
                # print(outputD_real[3,0,0,0])
                for real_cpu_path_i in range(len(real_cpu_path)):
                    file = config.waiting_reward_dir+real_cpu_path[real_cpu_path_i].split('/')[-1].split('__')[0]+'__waiting.npz'
                    np.savez(file,
                             gsa_reward=[(-outputD_real[real_cpu_path_i,0,0,0]+1.0)*0.1])

            # train D network with fake
            noise.resize_(opt.batchSize, nz, 1, 1).normal_(0, 1)
            noisev = Variable(noise, volatile = True) # totally freeze netG
            fake = Variable(netG(noisev).data)
            inputv = fake
            errD_fake, _ = netD(inputv)
            print('fake prob')
            print(errD_fake)
            errD_fake.backward(mone)
            errD = errD_real - errD_fake
            optimizerD.step()

        ######################################################################
        ####################### End of Update D network ######################
        ######################################################################

        ######################################################################
        ########################## Update G network ##########################
        ######################################################################

        '''
            when train G networks, paramters in p network is freezed
            to avoid computation on grad
            this is reset to true when training D network
        '''
        for p in netD.parameters():
            p.requires_grad = False

        netG.zero_grad()

        '''
            in case our last batch was the tail batch of the dataloader,
            make sure we feed a full batch of noise
        '''
        noise.resize_(opt.batchSize, nz, 1, 1).normal_(0, 1)
        noisev = Variable(noise)
        fake = netG(noisev)
        errG, _ = netD(fake)
        errG.backward(one)
        optimizerG.step()

        ######################################################################
        ###################### End of Update G network #######################
        ######################################################################


        ######################################################################
        ########################### One in dataloader ########################
        ######################################################################

        gen_iterations += 1

        '''log result'''
        print('[%d/%d][%d/%d][%d] Loss_D: %f Loss_G: %f Loss_D_real: %f Loss_D_fake %f'
            % (epoch, opt.niter, i, len(dataloader), gen_iterations,
            errD.data[0], errG.data[0], errD_real.data[0], errD_fake.data[0]))
        if gen_iterations % 500 == 0:
            real_cpu = real_cpu.mul(0.5).add(0.5)
            vutils.save_image(real_cpu, '{0}/real_samples.png'.format(opt.experiment))
            fake = netG(Variable(fixed_noise, volatile=True))
            fake.data = fake.data.mul(0.5).add(0.5)
            vutils.save_image(fake.data, '{0}/fake_samples_{1}.png'.format(opt.experiment, gen_iterations))

        ######################################################################
        ######################### End One in dataloader ######################
        ######################################################################

    ######################################################################
    ############################# One in epoch ###########################
    ######################################################################

    '''do checkpointing'''
    torch.save(netG.state_dict(), '{0}/netG_epoch_{1}.pth'.format(opt.experiment, epoch))
    torch.save(netD.state_dict(), '{0}/netD_epoch_{1}.pth'.format(opt.experiment, epoch))

    ######################################################################
    ########################## End One in epoch ##########################
    ######################################################################
