import torch
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.functional as F
from torch.utils.data import DataLoader
import nn.tools as tools
from tqdm import tqdm
from collections import OrderedDict

import nn.losses as losses_utils
import gzip
import csv


class CarvanaClassifier:
    def __init__(self, net, train_loader: DataLoader, valid_loader: DataLoader):
        self.net = net
        self.valid_loader = valid_loader
        self.train_loader = train_loader
        self.use_cuda = torch.cuda.is_available()

    def _criterion(self, logits, labels):
        # l = BCELoss2d()(logits, labels)
        l = losses_utils.BCELoss2d().forward(logits, labels) + losses_utils.SoftDiceLoss().forward(logits, labels)
        return l

    def _validate_epoch(self, threshold):
        losses = tools.AverageMeter()
        accuracies = tools.AverageMeter()

        it_count = len(self.valid_loader)
        batch_size = self.train_loader.batch_size
        with tqdm(total=it_count, desc="Validating", leave=False) as pbar:
            for ind, (images, target) in enumerate(self.valid_loader):
                if self.use_cuda:
                    images = images.cuda()
                    target = target.cuda()

                # Volatile because we are in pure inference mode
                # http://pytorch.org/docs/master/notes/autograd.html#volatile
                images = Variable(images, volatile=True)
                target = Variable(target, volatile=True)

                # forward
                logits = self.net(images)
                probs = F.sigmoid(logits)
                pred = (probs > threshold).float()

                loss = self._criterion(logits, target)
                acc = losses_utils.dice_loss(pred, target)
                losses.update(loss.data[0], batch_size)
                accuracies.update(acc.data[0], batch_size)
                pbar.update(1)

        return losses.avg, accuracies.avg

    def _train_epoch(self, epoch_id, epochs, optimizer, threshold):
        losses = tools.AverageMeter()
        accuracies = tools.AverageMeter()

        # Total training files count / batch_size
        batch_size = self.train_loader.batch_size
        it_count = len(self.train_loader)
        with tqdm(total=it_count,
                  desc="Epochs {}/{}".format(epoch_id + 1, epochs),
                  bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{remaining}{postfix}]'
                  ) as pbar:
            for ind, (inputs, target) in enumerate(self.train_loader):

                if self.use_cuda:
                    inputs = inputs.cuda()
                    target = target.cuda()
                inputs, target = Variable(inputs), Variable(target)

                # forward
                logits = self.net.forward(inputs)
                probs = F.sigmoid(logits)
                pred = (probs > threshold).float()

                # backward + optimize
                loss = self._criterion(logits, target)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

                # print statistics
                acc = losses_utils.dice_loss(pred, target)
                lr = tools.get_learning_rate(optimizer)[0]

                losses.update(loss.data[0], batch_size)
                accuracies.update(acc.data[0], batch_size)

                # Update pbar
                pbar.set_postfix(OrderedDict(loss='{0:1.5f}'.format(loss.data[0]), acc='{0:1.5f}'.format(acc.data[0])))
                pbar.update(1)
        return losses.avg, accuracies.avg

    def train(self, epochs, threshold=0.5):
        if self.use_cuda:
            self.net.cuda()
        optimizer = optim.SGD(self.net.parameters(), lr=0.01, momentum=0.9, weight_decay=0.0005)

        print("Training on {} samples and validating on {} samples "
              .format(len(self.train_loader.dataset), len(self.valid_loader.dataset)))
        for epoch_id, epoch in enumerate(range(epochs)):
            self.net.train()

            # Run a train pass on the current epoch
            train_loss, train_acc = self._train_epoch(epoch_id, epochs, optimizer, threshold)

            # switch to evaluate mode
            self.net.eval()

            valid_loss, valid_acc = self._validate_epoch(threshold)
            print("train_loss = {:03f}, train_acc = {:03f}\nval_loss   = {:03f}, val_acc   = {:03f}"
                  .format(train_loss, train_acc, valid_loss, valid_acc))

    def predict(self, test_loader, to_file=None, t_fnc=None, fnc_args=None):
        """
        Launch the prediction on the given loader and periodically
        store them in a csv file with gz compression if to_file is given.
        The results are stored in a list otherwise.
        :param test_loader: The loader containing the test dataset
        :param to_file: A gz file path or None if you want to get the prediction as array
        :param fnc_args: A list of arguments to pass to t_fnc
        :param t_fnc: A transformer function which takes in a single prediction array and
                    return a transformed result. The signature of the function must be:
                    t_fnc(prediction, *fnc_args) -> (transformed_prediction)
        :return: The prediction array (empty if to_file is given)
        """
        # Switch to evaluation mode
        self.net.eval()

        it_count = len(test_loader)
        predictions = []
        file = None
        writer = None

        if to_file:
            file = gzip.open(to_file, "wt", newline="")
            writer = csv.writer(file)
            writer.writerow(["img", "rle_mask"])

        with tqdm(total=it_count, desc="Classifying") as pbar:
            for ind, (images, files_name) in enumerate(test_loader):
                if self.use_cuda:
                    images = images.cuda()

                images = Variable(images, volatile=True)

                # forward
                logits = self.net(images)
                probs = F.sigmoid(logits)

                # Save the predictions
                for (pred, name) in zip(probs, files_name):
                    pred_arr = pred.data[0].cpu().numpy()

                    # Execute the transformer function
                    if t_fnc:
                        pred_arr = t_fnc(pred_arr, *fnc_args)

                    if file:
                        writer.writerow([name, pred_arr])
                    else:
                        predictions.append((name, pred_arr))

                pbar.update(1)

        if file:
            file.close()

        return predictions
