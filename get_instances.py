import os, shutil
import torch
import torch.nn as nn

def get_dirs(workspace, remake=False):
    #if path already exists, remove and make it again.
    if remake:
        if os.path.exists(workspace): shutil.rmtree(workspace)

    checkpoints_dir = os.path.join(workspace, 'checkpoints')
    os.makedirs(checkpoints_dir, exist_ok=True)

    log_dir = os.path.join(workspace, 'log.txt')

    return checkpoints_dir, log_dir

def get_dataset(dataset_name, dataset_params, mode, verbose=True):
   
    if dataset_name == 'fastmri_dataset':
        from modl_dataset import fastmri_dataset
        dataset = fastmri_dataset(mode=mode, **dataset_params)
    
    if dataset_name == 'ssdu_fastmri_dataset':
        from ssdu_dataset import ssdu_dataset
        dataset = ssdu_dataset(mode=mode, **dataset_params)
                                   
    if verbose:
        print('{} data: {}'.format(mode, len(dataset)))
    return dataset

def get_loaders(dataset_name, dataset_params, batch_size, modes, verbose=True):
    from torch.utils.data import DataLoader
    dataloaders = {}
    for mode in modes:
        dataset = get_dataset(dataset_name, dataset_params, mode, verbose)
        shuffle = True if mode == 'train' else False
        dataloaders[mode] = DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)

    return dataloaders

def get_model(model_name, model_params, device):
    if model_name == 'base_modl':
        from proj_models.modl import MoDL
        model = MoDL(**model_params)
        
    if model_name == 'base_modl_ssdu':
        from proj_models.modl import MoDL_ssdu
        model = MoDL_ssdu(**model_params)

    if model_name == 'base_varnet':
        from proj_models.varnet import VarNet
        model = VarNet(**model_params)
                
    # if device == 'cuda' and torch.cuda.device_count()>1:
    #     model = nn.DataParallel(model)
    model.to(device)
    return model

def get_loss(loss_name):
    if loss_name == 'MSE':
        return nn.MSELoss()

def get_score_fs(score_names):
    score_fs = {}
    for score_name in score_names:
        if score_name == 'PSNR':
            from utils import psnr_batch
            score_f = psnr_batch
        elif score_name == 'SSIM':
            from utils import ssim_batch
            score_f = ssim_batch

        score_fs[score_name] = score_f
    return score_fs

def get_optim_scheduler(optim_name, optim_params, scheduler_name, scheduler_params):
    import torch.optim as optim
    optimizer = getattr(optim, optim_name)(**optim_params)  # Ex: equivalent to optim.SGD(lr=0.01, momentum=0.9)
    if scheduler_name:
        scheduler = getattr(optim.lr_scheduler, scheduler_name)(optimizer, **scheduler_params)
        # Ex: optim.lr_scheduler.StepLR(optimizer, step_size=30, gamma=0.1)
    else:
        scheduler = None
    return optimizer, scheduler

def get_writers(tensorboard_dir, modes):
    from torch.utils.tensorboard import SummaryWriter
    writers = {}
    for mode in modes:
        tensorboard_path = os.path.join(tensorboard_dir, mode)
        if os.path.exists(tensorboard_path): shutil.rmtree(tensorboard_path)
        writers[mode] = SummaryWriter(tensorboard_path)
    return writers

class CheckpointSaver:
    def __init__(self, checkpoints_dir):
        self.checkpoints_dir = checkpoints_dir
        self.best_score = 0
        self.saved_epoch = 0

    def load(self, restore_path, prefix, model, optimizer, scheduler):
        checkpoint_path = [os.path.join(restore_path, f) for f in os.listdir(restore_path) if f.startswith(prefix)][0]
        if prefix == 'inter':
            start_epoch, model, optimizer, scheduler = self.load_checkpoints(checkpoint_path, model, optimizer, scheduler)
        elif prefix in ['best', 'final']:
            model = self.load_model(checkpoint_path, model)
            start_epoch = 0
        else:
            raise NotImplementedError
        return start_epoch, model, optimizer, scheduler

    def load_checkpoints(self, restore_path, model, optimizer, scheduler):
        print('loading checkpoints from {}...'.format(restore_path))
        checkpoints = torch.load(restore_path)

        model.load_state_dict(checkpoints['model_state_dict'])
        optimizer.load_state_dict(checkpoints['optim_state_dict'])
        #print('loading optimizer {}'.format(optimizer))
        
        if scheduler:
            scheduler = scheduler.load_state_dict(checkpoints['scheduler_state_dict'])

        self.best_score = checkpoints['best_score']
        start_epoch = checkpoints['epoch'] + 1
        return start_epoch, model, optimizer, scheduler

    def load_model(self, restore_path, model):
        print('loading model from {}...'.format(restore_path))
        state_dict = torch.load(restore_path)
        model.load_state_dict(state_dict)
        return model

    def save_checkpoints(self, epoch, model, optimizer, scheduler):
        torch.save({
            'model_state_dict': model.module.state_dict() if isinstance(model, nn.DataParallel) else model.state_dict(),
            'epoch': epoch,
            'optim_state_dict': optimizer.state_dict(),
            'scheduler_state_dict': scheduler.state_dict() if scheduler else None,
            'best_score': self.best_score
        }, os.path.join(self.checkpoints_dir, 'inter.pth'))

    def save_model(self, model, current_score, current_epoch, final):
        model_state_dict = model.module.state_dict() if isinstance(model, nn.DataParallel) else model.state_dict()

        if final:
            # Remove existing 'final_' files
            for f in os.listdir(self.checkpoints_dir):
                
                if f.startswith('final'): os.remove(os.path.join(self.checkpoints_dir, f))
                    
                model_path = os.path.join(self.checkpoints_dir, 'final.epoch{:04d}-score{:.4f}.pth'.format(current_epoch, current_score))
                print('saving model to ...{}'.format(model_path))
                torch.save(model_state_dict, model_path)
                self.best_score = current_score
                self.saved_epoch = current_epoch
        else:
            if current_score >= self.best_score:
                #prev_model = [f for f in os.listdir(self.checkpoints_dir) if f.startswith('best')][0]
                prev_models = [f for f in os.listdir(self.checkpoints_dir) if f.startswith('best')]

                # Check if the list is empty
                if prev_models:
                    prev_model = prev_models[0]  # Take the first file in the list
                else:
                    prev_model = None
                
                #os.remove(os.path.join(self.checkpoints_dir, prev_model))
                if prev_model is not None:
                    file_path = os.path.join(self.checkpoints_dir, prev_model)
                    if os.path.exists(file_path):  # Check if the file exists before trying to remove it
                        os.remove(file_path)
                        
                model_path = os.path.join(self.checkpoints_dir, 'best.epoch{:04d}-score{:.4f}.pth'.format(current_epoch, current_score))
                print('saving model to ...{}'.format(model_path))
                torch.save(model_state_dict, model_path)
                self.best_score = current_score
                self.saved_epoch = current_epoch