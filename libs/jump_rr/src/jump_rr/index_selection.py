#!/usr/bin/env jupyter

import cupy as cp


def get_bottom_top_indices(mat: cp.array, n: int, skip_first=False) -> cp.array:
    # Get the top n and bottom n indices from a matrix for each row.
    mask = cp.ones(mat.shape[1], dtype=bool)
    mask[n + skip_first - 1 : -n - 1] = False
    if skip_first:
        mask[0] = False

    indices = mat.argsort(axis=1)[:, mask]

    xs = cp.indices(indices.shape)[0].flatten().get()
    ys = indices.flatten().get()
    return xs, ys
