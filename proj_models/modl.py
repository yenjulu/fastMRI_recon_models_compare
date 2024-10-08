import torch
import torch.nn as nn

from utils import r2c, c2r, fft_torch
from proj_models import mri, networks


#CNN denoiser ======================
def conv_block(in_channels, out_channels):
    return nn.Sequential(
        nn.Conv2d(in_channels, out_channels, 3, padding=1),
        nn.BatchNorm2d(out_channels),
        nn.ReLU()
    )

class cnn_denoiser(nn.Module):
    def __init__(self, n_layers):
        super().__init__()
        layers = []
        layers += conv_block(2, 64)

        for _ in range(n_layers-2):  # n_layers : 1,2 will not execute
            layers += conv_block(64, 64)

        layers += nn.Sequential(
            nn.Conv2d(64, 2, 3, padding=1),
            nn.BatchNorm2d(2)
        )

        self.nw = nn.Sequential(*layers)

    def forward(self, x):
        idt = x # (2, nrow, ncol)
        dw = self.nw(x) + idt # (2, nrow, ncol)
        return dw

#CG algorithm ======================
class myAtA(nn.Module):
    """
    performs DC step
    """
    def __init__(self, csm, mask, lam):
        super(myAtA, self).__init__()
        self.csm = csm # complex (B x ncoil x nrow x ncol)
        self.mask = mask # complex (B x nrow x ncol)
        self.lam = lam

        self.A = mri.SenseOp(csm, mask)

    def forward(self, im): #step for batch image
        """
        :im: complex image (B x nrow x nrol)
        """
        im_u = self.A.adj(self.A.fwd(im))    # im_u = A*A(x)
        return im_u + self.lam * im   # A*A(x) + λx = AtA(x)

def myCG(AtA, rhs):  # inputs : A , b => A: AtA, b: rhs
    """
    performs CG algorithm
    :AtA: a class object that contains csm, mask and lambda and operates forward model
    """
    rhs = r2c(rhs, axis=1) # nrow, ncol
    x = torch.zeros_like(rhs)
    i, r, p = 0, rhs, rhs   # r =rhs - AtA(x) = rhs.  
    rTr = torch.sum(r.conj()*r).real
    while i < 10 and rTr > 1e-10:           # i < 10
        Ap = AtA(p)  # AtA() 
        alpha = rTr / torch.sum(p.conj()*Ap).real
        x = x + alpha * p
        r = r - alpha * Ap
        rTrNew = torch.sum(r.conj()*r).real
        beta = rTrNew / rTr
        p = r + beta * p
        i += 1
        rTr = rTrNew
    return c2r(x, axis=1)

class data_consistency(nn.Module):
    def __init__(self):
        super().__init__()
        self.lam = nn.Parameter(torch.tensor(0.05), requires_grad=True)

    def forward(self, z_k, x0, csm, mask):
        rhs = x0 + self.lam * z_k # (2, nrow, ncol)   rhs = At*b + λz
        AtA = myAtA(csm, mask, self.lam)   
        rec = myCG(AtA, rhs)  # AtA(rec) = rhs
        return rec

class MoDL(nn.Module):
    def __init__(self, n_layers, k_iters):
        """
        :n_layers: number of layers
        :k_iters: number of iterations
        """
        super().__init__()
        self.k_iters = k_iters
        self.dw = cnn_denoiser(n_layers)
        self.dc = data_consistency()  

    def forward(self, x0, csm, mask):
        """
        :x0: zero-filled reconstruction (B, 2, nrow, ncol) - float32
        :csm: coil sensitivity map (B, ncoil, nrow, ncol) - complex64
        :mask: sampling mask (B, nrow, ncol) - int8
        """
        x_k = x0.clone()
        for k in range(self.k_iters):
            # cnn denoiser
            z_k = self.dw(x_k) # (nbatch, 2, nrow, ncol)
           
            # data consistency
            x_k = self.dc(z_k, x0, csm, mask) # (nbatch, 2, nrow, ncol)
        return r2c(x_k, axis=1), r2c(z_k, axis=1)  # x_k, z_k 


class MoDL_ssdu(nn.Module):
    def __init__(self, n_layers, k_iters):
        """
        :n_layers: number of layers
        :k_iters: number of iterations
        """
        super().__init__()
        self.k_iters = k_iters
        self.dw = networks.ResNet(n_layers)
        self.dc = data_consistency()
       
    def forward(self, x0, csm, trn_mask, loss_mask):
        """
        :x0: zero-filled reconstruction (B, 2, nrow, ncol) - float32
        :csm: coil sensitivity map (B, ncoil, nrow, ncol) - complex64
        :mask: sampling mask (B, nrow, ncol) - int8
        """

        x_k = x0.clone()
        for k in range(self.k_iters):
            # resnet
            z_k = self.dw(x_k) # (B, 2, nrow, ncol)
                      
            # data consistency
            x_k = self.dc(z_k, x0, csm, trn_mask) # (B, 2, nrow, ncol)
            
        kspace_x_k = self.SSDU_kspace(x_k, csm, loss_mask)
            
        return r2c(x_k, axis=1), kspace_x_k, r2c(z_k, axis=1)

    def SSDU_kspace(self, img, csm, loss_mask):
        """
        Transforms unrolled network output to k-space
        and selects only loss mask locations(\Lambda) for computing loss
        :img: zero-filled reconstruction (B, 2, nrow, ncol) - float32
        :csm: coil sensitivity map (B, ncoil, nrow, ncol) - complex64
        :loss_mask: sampling mask (B, nrow, ncol) - int8               
        """
        img = r2c(img, axis=1) # (B, 2, nrow, ncol)  ---> (B, nrow, ncol)
        csm = torch.swapaxes(csm, 0, 1)  # (coils, B, nrow, ncol)
        coil_imgs = csm * img
        
        #kspace = mri.fftc(coil_imgs, axes=(-2, -1), norm='ortho')
        kspace = fft_torch(coil_imgs, axes=(-2, -1), norm=None, unitary_opt=True)       
        output = torch.swapaxes(loss_mask * kspace, 0, 1)

        return c2r(output, axis=1)  # B x 2 x coils x nrow x ncol