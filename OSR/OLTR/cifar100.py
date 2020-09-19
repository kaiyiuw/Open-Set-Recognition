
from __future__ import print_function

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

import torchvision
import numpy as np
import torchvision.transforms as transforms

import os
import argparse
import sys
#from models import *
sys.path.append("../..")
import backbones.cifar as models
from datasets import CIFAR100
from Utils import adjust_learning_rate, progress_bar, Logger, mkdir_p, Evaluation
from netbuilder import Network

model_names = sorted(name for name in models.__dict__
    if not name.startswith("__")
    and callable(models.__dict__[name]))

os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"
# os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
parser = argparse.ArgumentParser(description='PyTorch CIFAR10 Training')

# Dataset preperation
parser.add_argument('--train_class_num', default=50, type=int, help='Classes used in training')
parser.add_argument('--test_class_num', default=100, type=int, help='Classes used in testing')
parser.add_argument('--includes_all_train_class', default=True,  action='store_true',
                    help='If required all known classes included in testing')

# Others
parser.add_argument('--arch', default='ResNet18', choices=model_names, type=str, help='choosing network')
parser.add_argument('--bs', default=256, type=int, help='batch size')
parser.add_argument('--lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--evaluate', action='store_true', help='Evaluate without training')

# Parameters for stage 1
parser.add_argument('--stage1_resume', default='', type=str, metavar='PATH', help='path to latest checkpoint')
parser.add_argument('--stage1_es', default=30, type=int, help='epoch size')
parser.add_argument('--stage1_use_fc', default=False,  action='store_true',
                    help='If to use the last FC/embedding layer in network, FC (whatever, stage1_feature_dim)')
parser.add_argument('--stage1_feature_dim', default=512, type=int, help='embedding feature dimension')
parser.add_argument('--stage1_classifier', default='dotproduct', type=str,choices=['dotproduct', 'cosnorm', 'metaembedding'],
                    help='Select a classifier (default dotproduct)')




args = parser.parse_args()
device = 'cuda' if torch.cuda.is_available() else 'cpu'
args.checkpoint = './checkpoints/cifar/' + args.arch
if not os.path.isdir(args.checkpoint):
    mkdir_p(args.checkpoint)

print('==> Preparing data..')
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

trainset = CIFAR100(root='../../data', train=True, download=True, transform=transform_train,
                    train_class_num=args.train_class_num, test_class_num=args.test_class_num,
                    includes_all_train_class=args.includes_all_train_class)

testset = CIFAR100(root='../../data', train=False, download=True, transform=transform_test,
                   train_class_num=args.train_class_num, test_class_num=args.test_class_num,
                   includes_all_train_class=args.includes_all_train_class)




def main():
    print(device)
    net = main_stage1()
    cal_centroids(net, device)


def main_stage1():
    print(f"\nStart Stage-1 training...\n")
    start_epoch = 0  # start from epoch 0 or last checkpoint epoch
    # data loader
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.bs, shuffle=True, num_workers=4)
    # testloader = torch.utils.data.DataLoader(testset, batch_size=args.bs, shuffle=False, num_workers=4)

    # Model
    print('==> Building model..')
    net = Network(backbone=args.arch, embed_dim=512, num_classes=args.train_class_num,
                 use_fc=False, attmodule=False, classifier='dotproduct', backbone_fc=False, data_shape=4)
    # net = models.__dict__[args.arch](num_classes=args.train_class_num) # CIFAR 100
    net = net.to(device)

    if device == 'cuda':
        net = torch.nn.DataParallel(net)
        cudnn.benchmark = True

    if args.stage1_resume:
        # Load checkpoint.
        if os.path.isfile(args.stage1_resume):
            print('==> Resuming from checkpoint..')
            checkpoint = torch.load(args.stage1_resume)
            net.load_state_dict(checkpoint['net'])
            # best_acc = checkpoint['acc']
            # print("BEST_ACCURACY: "+str(best_acc))
            start_epoch = checkpoint['epoch']
            logger = Logger(os.path.join(args.checkpoint, 'log_stage1.txt'), resume=True)
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    else:
        logger = Logger(os.path.join(args.checkpoint, 'log_stage1.txt'))
        logger.set_names(['Epoch', 'Learning Rate', 'Train Loss','Train Acc.'])

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)

    for epoch in range(start_epoch, start_epoch + args.stage1_es):
        print('\nStage_1 Epoch: %d   Learning rate: %f' % (epoch+1, optimizer.param_groups[0]['lr']))
        adjust_learning_rate(optimizer, epoch, args.lr,step=10)
        train_loss, train_acc = stage1_train(net,trainloader,optimizer,criterion,device)
        save_model(net, None, epoch, os.path.join(args.checkpoint,'stahe_1_last_model.pth'))
        logger.append([epoch+1, optimizer.param_groups[0]['lr'], train_loss, train_acc])
    logger.close()
    print(f"\nFinish Stage-1 training...\n")
    return net


# Training
def stage1_train(net,trainloader,optimizer,criterion,device):
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs, fea, featmaps = net(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        optimizer.step()

        train_loss += loss.item()
        _, predicted = outputs.max(1)
        total += targets.size(0)
        correct += predicted.eq(targets).sum().item()

        progress_bar(batch_idx, len(trainloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
            % (train_loss/(batch_idx+1), 100.*correct/total, correct, total))
    return train_loss/(batch_idx+1), correct/total


# calculate centroids
def cal_centroids(net,device):
    print(f"Calculating centroids ...")
    # data loader
    trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.bs, shuffle=True, num_workers=4)

    net.eval()
    centroids = torch.zeros([args.train_class_num,args.stage1_feature_dim]).to(device)
    class_count = torch.zeros([args.train_class_num,1]).to(device)
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(trainloader):
            inputs, targets = inputs.to(device), targets.to(device)
            # outputs, _, _ = net(inputs)
            _, features, _ = net(inputs)
            print(f"targets.shape: {targets.shape}")
            print(f"outputs.shape: {features.shape}")
            for i in range(0,targets.size(0)):
                label = targets[i]
                class_count[label] += 1
                centroids[label] += features[i, :]
    centroids = centroids/(class_count.expand_as(centroids))
    print(centroids.shape)


def test(epoch, net,trainloader,  testloader,criterion, device):
    net.eval()

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = net(inputs)
            progress_bar(batch_idx, len(testloader))


def save_model(net, acc, epoch, path):
    state = {
        'net': net.state_dict(),
        'testacc': acc,
        'epoch': epoch,
    }
    torch.save(state, path)

if __name__ == '__main__':
    main()

