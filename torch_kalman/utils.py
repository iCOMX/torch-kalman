from typing import Dict, Union, Any, Optional

import torch
from pandas import Series

import numpy as np
from torch import Tensor


def matrix_diag(diagonal: Tensor) -> Tensor:
    N = diagonal.shape[-1]
    shape = diagonal.shape[:-1] + (N, N)
    device, dtype = diagonal.device, diagonal.dtype
    result = torch.zeros(shape, dtype=dtype, device=device)
    indices = torch.arange(result.numel(), device=device).reshape(shape)
    indices = indices.diagonal(dim1=-2, dim2=-1)
    result.view(-1)[indices] = diagonal
    return result


def fourier_model_mat(dt: Union[np.ndarray, Series],
                      K: int,
                      period: Union[np.timedelta64, str],
                      start_dt: Optional[np.datetime64] = None) -> np.ndarray:
    # parse period:
    if isinstance(period, str):
        if period == 'weekly':
            period = np.timedelta64(7, 'D')
        elif period == 'yearly':
            period = np.timedelta64(int(365.25 * 24), 'h')
        elif period == 'daily':
            period = np.timedelta64(24, 'h')
        else:
            raise ValueError("Unrecognized `period`.")

    # convert datetimes and period into ints:
    if isinstance(dt, Series):
        dt = dt.values
    time_unit = np.datetime_data(period)[0]
    if start_dt is None:
        time = dt.astype(f'datetime64[{time_unit}]').view('int64')
    else:
        time = (dt - start_dt).astype(f'timedelta64[{time_unit}]').view('int64')
    period_int = period.view('int64')

    # fourier matrix:
    out = np.empty((len(dt), K * 2))
    for idx in range(K):
        k = idx + 1
        for sincos in range(2):
            val = 2. * np.pi * k * time / period_int
            out[:, idx * 2 + sincos] = np.sin(val) if sincos == 0 else np.cos(val)
    return out


def itervalues_sorted_keys(adict: Dict):
    for k in sorted(adict.keys()):
        yield adict[k]


def dict_key_replace(obj: Union[Dict, Any], old: str, new: str) -> Dict:
    if not isinstance(obj, Dict):
        return obj

    out = {}
    for key, value in obj.items():
        if key == old:
            out[new] = dict_key_replace(value, old=old, new=new)
        else:
            out[key] = dict_key_replace(value, old=old, new=new)
    return out


def zpad(x: Any, n: int) -> str:
    return str(x).rjust(n, "0")


def split_flat(tens: Tensor, dim: int, clone: bool = False):
    if clone:
        return [tens.select(dim, i).clone() for i in range(tens.shape[dim])]
    else:
        assert not tens.requires_grad
        return [tens.select(dim, i) for i in range(tens.shape[dim])]


def identity(x: Any) -> Any:
    return x
