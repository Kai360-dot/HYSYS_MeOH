import numpy as np
import torch
import scipy
import torch.nn as nn

# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def generate_sobol_points(n: int, bounds):
    """
    n: number of points to sample
    """
    d = bounds.shape[1]
    m = int(np.ceil(np.log2(n)))
    sbsam = scipy.stats.qmc.Sobol(d=d, seed=42)
    rand_norm = sbsam.random_base2(m=m) # 2^m points
    pts = rand_norm * (bounds[1] - bounds[0]) + bounds[0]
    return pts[:n]

# ANN training template
class Surrogate:
    """Plain-numpy MLP: scaling folded into first/last layer"""
    def __init__(self, W, b):
        self.W, self.b = W, b

    def __call__(self, x):
        x = np.asarray(x, dtype=float)
        single = x.ndim == 1
        h = np.atleast_2d(x)
        for i, (Wl, bl) in enumerate(zip(self.W, self.b)):
            h = h @ Wl.T + bl
            if i + 1 < len(self.W):
                h = np.tanh(h)
        return h[0] if single else h

    def export(self, path):
        with open(path, "w") as f:
            f.write(f"{len(self.W)}\n")
            for Wl, bl in zip(self.W, self.b):
                f.write(f"{Wl.shape[1]} {Wl.shape[0]}\n")
                for row in Wl:
                    f.write(" ".join(f"{v:.17g}" for v in row) + "\n")
                f.write(" ".join(f"{v:.17g}" for v in bl) + "\n")

def fit_surrogate(X, Y, hid=16, epochs=3000, lr=1e-3, seed=0, verbose=True):
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    if Y.ndim == 1:
        Y = Y[:, None]                       # tolerate 1-D targets
    torch.manual_seed(seed)

    xmu, xsig = X.mean(0), X.std(0) + 1e-12
    ymu, ysig = Y.mean(0), Y.std(0) + 1e-12
    tx = torch.tensor((X - xmu) / xsig, dtype=torch.float64)
    ty = torch.tensor((Y - ymu) / ysig, dtype=torch.float64)

    net = nn.Sequential(
        nn.Linear(X.shape[1], hid), nn.Tanh(),
        nn.Linear(hid, hid), nn.Tanh(),
        nn.Linear(hid, Y.shape[1]),
    ).double()
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    for ep in range(epochs):
        opt.zero_grad()
        loss = nn.functional.mse_loss(net(tx), ty)
        loss.backward()
        opt.step()
        if verbose and ep % 500 == 0:
            print(f"  epoch {ep:5d} mse(std) {loss.item():.3e}")

    # fold standardization into affine layers -> raw-unit in, raw-unit out
    lins = [m for m in net if isinstance(m, nn.Linear)]
    W = [m.weight.detach().numpy().copy() for m in lins]
    b = [m.bias.detach().numpy().copy() for m in lins]
    b[0] = b[0] - W[0] @ (xmu / xsig)
    W[0] = W[0] / xsig[None, :]
    W[-1] = ysig[:, None] * W[-1]
    b[-1] = ysig * b[-1] + ymu
    return Surrogate(W, b)