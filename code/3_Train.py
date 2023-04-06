from __future__ import print_function

import json
import os
import signal

import numpy as np
import dvc.api
import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.utils.data
from tqdm import tqdm

from CVAE.model import CVAE
from utils import loadTrain, loadValid, idTo1Hot
from torch.optim.lr_scheduler import ExponentialLR

torch.set_default_tensor_type(torch.FloatTensor)
torch.manual_seed(42)

signal_received = False

def handle_interrupt(signal_number, frame):
    global signal_received
    signal_received = True
    
signal.signal(signal.SIGINT, handle_interrupt)

def loadTrainData(folderPath):
    print('loading training data...')
    trainTensors = loadTrain(folderPath)
    print('loading validation data...')
    validTensors = loadValid(folderPath)
    print('done!')
    
    return trainTensors, validTensors

def cvaeLoss(x, xHat, mu, logvar):
    RECON = F.cross_entropy(xHat, x)
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())
    return RECON + KLD

def prepareInputs(data, vocabSize):
    embedding = np.array([idTo1Hot(i, vocabSize) for i in list(data[0])])
    embedding = torch.Tensor(embedding)
    embedding = embedding.to(device)

    assay = data[1]
    assay = torch.Tensor(assay)
    assay = assay.to(device)
    
    value = data[2]
    value = torch.Tensor(value)
    value = value.to(device)
    
    labels = torch.cat((assay, value), dim=0)
    
    return embedding, labels


def evaluate(model, validTensors):
    model.eval()
    evalLoss = []
    for data in tqdm(validTensors):
        
        embedding, labels = prepareInputs(data, model.vocabSize)
        
        with torch.no_grad():
            xHat, z_mean, z_logvar = model(embedding, labels)
            loss = cvaeLoss(embedding, xHat, z_mean, z_logvar)
            evalLoss.append(loss.item())
            
        if signal_received:
            print('Stopping actual validation step...')
            break
        
    return sum(evalLoss) / len(evalLoss)
    

def train(model, optimizer, scheduler, folderPath, otuputFolder, epochs=5):
    trainTensors, validTensors = loadTrainData(folderPath)
    
    checkpointId = 0
    bestTrainLoss = np.inf
    bestEvalLoss = np.inf
    
    for epoch in range(epochs):
        print('{}/{}'.format(epoch, epochs))
        epochLoss = []
        for data in tqdm(trainTensors):
            embedding, labels = prepareInputs(data, model.vocabSize)
            
            xHat, z_mean, z_logvar = model(embedding, labels)
            loss = cvaeLoss(embedding, xHat, z_mean, z_logvar)
            epochLoss.append(loss.item())
            
            loss.backward()
            optimizer.step()
            
            if signal_received:
                print('Stopping actual train step...')
                break
            
        scheduler.step()
        
        epochLoss = sum(epochLoss)/len(epochLoss)
        evalLoss = evaluate(model, validTensors)
        
        with open('{}loss.tsv'.format(metricsOutPath), 'a+') as f:
            f.write('{}\t{}\t{}\n'.format(epoch, epochLoss, 0))###########
            
        if epochLoss < bestTrainLoss and evalLoss < bestEvalLoss:
            bestTrainLoss = epochLoss
            bestEvalLoss = evalLoss
            
            torch.save(model.state_dict(),'{}checkpoint{}epoch{}.pt'.format(otuputFolder, checkpointId, epoch))
            torch.save(model.state_dict(),'{}bestModel.pt'.format(otuputFolder))
            
            checkpointId += 1
            
        if signal_received:
            print('Stopping training...')
            break
    
    


if __name__ == '__main__':
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    params = dvc.api.params_show()
    
    latentDim = params['latentDim']
    
    metricsOutPath = params['training']['metricsOutPath']
    processedDataPath = params['training']['processedData']
    outModelFolder = params['training']['outModelFolder']
    epochs = params['training']['epochs']
    
    os.makedirs(outModelFolder, exist_ok=True)
    os.makedirs(metricsOutPath, exist_ok=True)
    
    with open('{}loss.tsv'.format(metricsOutPath), 'w') as f:
        f.write('step\ttLoss\teLoss\n')
    
    with open('{}/modelInfo.json'.format(processedDataPath)) as f:
        modelInfo = json.load(f)
        
    embeddingSize = modelInfo['embeddingSize']
    vocabSize = modelInfo['vocabSize']
    assaySize = modelInfo['assaySize']
    
    labelsSize = assaySize + 1
    
    model = CVAE(embeddingSize, vocabSize, labelsSize, latentDim)
    model = model.to(device)
    optimizer = optim.Adam(model.parameters(), lr=0.000001)
    scheduler = ExponentialLR(optimizer, gamma=0.96, last_epoch=-1)
    
    train(model, optimizer, scheduler, processedDataPath, outModelFolder, epochs)