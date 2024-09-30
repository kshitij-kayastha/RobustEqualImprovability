import tqdm
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
from copy import deepcopy
from typing import Callable
from types import SimpleNamespace
from torch.utils.data import DataLoader, random_split

from src.model import LR, NN
from src.effort import Effort
from src.data import FairnessDataset
from src.utils import model_performance


def fair_batch_proxy(Z: torch.tensor, Y_hat_max: torch.tensor):
    proxy_value = torch.tensor(0.)
    loss_fn = torch.nn.MSELoss(reduction='mean')

    loss_mean = loss_fn(Y_hat_max, torch.ones(len(Y_hat_max)))
    for z in [0,1]:
        z = int(z)
        group_idx = (Z==z)
        if group_idx.sum() == 0:
            loss_z = 0
        else:
            loss_z = loss_fn(Y_hat_max[group_idx], torch.ones(group_idx.sum()))
        proxy_value += torch.abs(loss_z - loss_mean)
    return proxy_value

def covariance_proxy(Z: torch.tensor, Y_hat_max: torch.tensor):
    proxy_value = torch.square(torch.mean((Z-Z.mean())*Y_hat_max))
    return proxy_value

class EIModel():
    def __init__(self, model: LR | NN, proxy: Callable, effort: Effort, tau: float = 0.5) -> None:
        self.model = model
        self.proxy = proxy
        self.effort = effort
        self.tau = tau
        self.train_history = SimpleNamespace()
        
    def train(self, 
              dataset: FairnessDataset, 
              lamb: float,
              alpha: float = 0.,
              lr: float = 1e-3,
              n_epochs: int = 100,
              batch_size: int = 1024,
              abstol: float = 1e-7,
              pga_n_iters: int = 50 
              ):

        generator = torch.Generator().manual_seed(0)
        data_loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, generator=generator)
        
        loss_fn = torch.nn.BCELoss(reduction='mean')
        optimizer = optim.Adam(self.model.parameters(), lr=lr, weight_decay=1e-4)
        
        # THIS IS FOR CONVERGENCE
        loss_diff = 1.
        prev_loss = torch.tensor(0.)
        
        for epoch in tqdm.trange(n_epochs, desc=f"Training [alpha={alpha:.3f}; lambda={lamb:.5f}; delta={self.effort.delta:.3f}]", unit="epochs", colour='#0091ff'):
            
            batch_losses = []
            curr_alpha = alpha * (epoch / n_epochs)

            for _, (X_batch, Y_batch, Z_batch) in enumerate(data_loader):
                Y_hat = self.model(X_batch).reshape(-1)
                
                batch_pred_loss = loss_fn(Y_hat, Y_batch)
                batch_loss = (1-lamb)*batch_pred_loss
                
                batch_fair_loss = 0
                if torch.sum(Y_hat<self.tau) > 0:
                    X_batch_e = X_batch[(Y_hat<self.tau), :]
                    Z_batch_e = Z_batch[(Y_hat<self.tau)]
                    
                    X_hat_max = self.effort(self.model, dataset, X_batch_e)

                    for module in self.model.layers:
                        if hasattr(module, 'weight'):
                            weight_min = module.weight.data - curr_alpha
                            weight_max = module.weight.data + curr_alpha
                        if hasattr(module, 'bias'):
                            bias_min = module.bias.data - curr_alpha
                            bias_max = module.bias.data + curr_alpha
                            
                    model_adv = deepcopy(self.model).bounded_init((weight_min, weight_max), (bias_min, bias_max))
                    optimizer_adv = optim.Adam(model_adv.parameters(), lr=1e-3, maximize=True)
                            
                    loss_diff_pga = 1.
                    fair_loss_pga = torch.tensor(0.)

                    for _ in range(pga_n_iters):
                        prev_loss = fair_loss_pga.clone().detach()
                        
                        Y_hat_max_pga = model_adv(X_hat_max).reshape(-1)
                        fair_loss_pga = self.proxy(Z_batch_e, Y_hat_max_pga)
        
                        optimizer_adv.zero_grad()
                        fair_loss_pga.backward()
                        optimizer_adv.step()
        
                        loss_diff_pga = (prev_loss - fair_loss_pga).abs()
                        
                        for module in model_adv.layers:
                            if hasattr(module, 'weight'):
                                module.weight.data = module.weight.data.clamp(weight_min, weight_max)
                            if hasattr(module, 'bias'):
                                module.bias.data = module.bias.data.clamp(bias_min, bias_max)
                        
                        if loss_diff_pga < abstol:
                            break
                    
                    Y_hat_max = model_adv(X_hat_max).reshape(-1)
                    batch_fair_loss = self.proxy(Z_batch_e, Y_hat_max)
                
                batch_loss += lamb*batch_fair_loss
                
                optimizer.zero_grad()
                if torch.isnan(batch_loss).any():
                    continue
                batch_loss.backward()
                optimizer.step()
        
                batch_losses.append(batch_loss.item())
            
            # THIS IS FOR CONVERGENCE (I'm not sure if this is correct)
            loss_diff = torch.abs(torch.mean(batch_losses)-prev_loss)
            
            if loss_diff < abstol:
                print(f'batch loss: {batch_loss.item()}')
                print(f'loss diff: {loss_diff.item()}')
                return self
            
        prev_loss = torch.mean(batch_losses).clone().detach()
        print(batch_loss.item())
        print(loss_diff)
        
        return self
        
        
    def predict(self,
                dataset: FairnessDataset,
                alpha: float,
                abstol: float = 1e-7,
                pga_n_iters: int = 50
                ):
        
        loss_fn = torch.nn.BCELoss(reduction='mean')
    
        Y_hat = self.model(dataset.X).reshape(-1)
        pred_loss =  loss_fn(Y_hat, dataset.Y)
        
        if torch.sum(Y_hat<self.tau) > 0:
            X_e = dataset.X[(Y_hat<self.tau).reshape(-1),:]
            Z_e = dataset.Z[(Y_hat<self.tau)]
            
            X_hat_max = self.effort(self.model, dataset, X_e)
            
            for module in self.model.layers:
                if hasattr(module, 'weight'):
                    weight_min = module.weight.data - alpha
                    weight_max = module.weight.data + alpha
                if hasattr(module, 'bias'):
                    bias_min = module.bias.data.item() - alpha
                    bias_max = module.bias.data.item() + alpha
            
            model_adv = deepcopy(self.model).bounded_init((weight_min, weight_max), (bias_min, bias_max))
            optimizer_adv = optim.Adam(model_adv.parameters(), lr=1e-3, maximize=True)
            
            loss_diff = 1.
            fair_loss = torch.tensor(0.)
            for _ in range(pga_n_iters):
                prev_loss = fair_loss.clone().detach()
                Y_hat_max = model_adv(X_hat_max).reshape(-1)
                fair_loss = self.proxy(Z_e, Y_hat_max)
                
                optimizer_adv.zero_grad()
                fair_loss.backward()
                optimizer_adv.step()
                
                loss_diff = (prev_loss - fair_loss).abs()
                
                for module in model_adv.layers:
                    if hasattr(module, 'weight'):
                        module.weight.data = module.weight.data.clamp(weight_min, weight_max)
                    if hasattr(module, 'bias'):
                        module.bias.data = module.bias.data.clamp(bias_min, bias_max)

                if loss_diff < abstol:
                    break
            self.model_adv = model_adv
        else:
            fair_loss = torch.tensor([0.]).float()
            self.model_adv = deepcopy(self.model)
        
        Y_hat = Y_hat.detach().float().numpy()
        # X_hat_max = self.effort(self.model, dataset, dataset.X)
        Y_hat_max = self.model_adv(X_hat_max).reshape(-1).detach().float().numpy()
        pred_loss = pred_loss.detach().item()
        fair_loss = fair_loss.detach().item()
        
        return Y_hat, Y_hat_max, pred_loss, fair_loss