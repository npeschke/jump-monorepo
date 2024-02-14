#!/usr/bin/env jupyter
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.15.2
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

"""
Generate aggregated statistics for feature values
Based on discussion https://github.com/broadinstitute/2023_12_JUMP_data_only_vignettes/issues/4#issuecomment-1918019212

1. Calculate the p value of all features
2. then adjust the p value to account for multiple testing https://www.statsmodels.org/dev/generated/statsmodels.stats.multitest.multipletests.html (fdr_bh)
3. Group features based on their hierarchy and compute combined p-value per group using https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.combine_pvalues.html (fisher); this will give you a p-value per group
4. then correct the p values from step 5 (fdr_bh)
"""

from math import sqrt
from pathlib import Path

try:
    import cupy as cp
except Exception:
    import numpy as cp

import numpy as np
import polars as pl
from scipy.stats import t  # TODO try to reimplement on cupy


def sample_ids(
    df: pl.DataFrame,
    column: str = "Metadata_JCP2022",
    n: int = 100,
    negcons: bool = True,
    seed: int = 42,
) -> pl.DataFrame:
    """Sample all occurrences of n ids in a given column, adding their negative controls."""
    identifiers = (
        precor.filter(pl.col("Metadata_pert_type") != pl.lit("negcon"))
        .get_column(column)
        .sample(n)
    )
    index_filter = pl.col(column).is_in(identifiers)
    if negcons:
        unique_plates = (
            (
                df.filter(pl.col(column).is_in(identifiers)).select(
                    pl.col("Metadata_Plate")
                )
            )
            .to_series()
            .unique()
        )
        index_filter = index_filter | (
            pl.col("Metadata_Plate").is_in(unique_plates)
            & (pl.col("Metadata_pert_type") == pl.lit("negcon"))
        )
    result = df.filter(index_filter)
    return result


def group_by_trt(
    df: pl.DataFrame, column: str = "Metadata_JCP2022"
) -> dict[str, tuple[pl.DataFrame, pl.DataFrame]]:
    """
    Partition a dataframe by using identifier column (by default Metadata_JCP2022)
    and then further split into two dataframes, one for positive controls and one
    for negative controls.
    """
    pos_partition, neg_partition = sampled.partition_by("Metadata_pert_type")
    ids_plates = dict(pos_partition.group_by(column).agg("Metadata_Plate").iter_rows())
    ids_prof = pos_partition.partition_by(column, as_dict=True, maintain_order=False)
    negcons = neg_partition.partition_by(
        "Metadata_Plate", as_dict=True, maintain_order=False
    )
    id_trt_negcon = {
        id_: (ids_prof[id_], pl.concat(negcons[plate] for plate in plates))
        for id_, plates in ids_plates.items()
    }
    return id_trt_negcon


def get_p_value(a, b, negcons_per_plate: int = 2, seed: int = 42):
    """
    Calculate the p value of two matrices in a column fashion.
    TODO check if we should sample independently or if we can sample once and used all features from a given sample set.
    Challenge:
    - Multiple genes likely share multiple negative controls

    Solution:
    1. Find gene
    2. Find its negative controls
    3. Sample negative controls
    4. Calculate p value of both distributions
    """
    if negcons_per_plate:  # Sample $negcons_per_plate elements from each plate
        b = b.filter(
            pl.int_range(0, pl.count()).shuffle(seed=seed).over("Metadata_Plate")
            < negcons_per_plate
        )
    # Convert relevant values to cupy
    matrix_a, matrix_b = [
        cp.array(x.select(pl.all().exclude("^Metadata.*$"))) for x in (a, b)
    ]

    # Calculate t statistic
    mean_a, mean_b = (matrix_a.mean(axis=0), matrix_b.mean(axis=0))
    std_a, std_b = matrix_a.std(axis=0, ddof=1), matrix_b.std(axis=0, ddof=1)
    n_a, n_b = (len(a), len(b))
    se_a, se_b = std_a / sqrt(n_a), std_b / sqrt(n_b)
    sed = cp.sqrt(se_a**2 + se_b**2)
    t_stat = (mean_a - mean_b) / sed
    # Calculate p value
    df = n_a + n_b - 2
    p = (1 - t.cdf((np.abs(t_stat).get()), df)) ** 2  # TODO cupy remove scipy dep
    return p


# %% Testing zone

# %% Loading
from tqdm import tqdm

dir_path = Path("/dgx1nas1/storage/data/shared/morphmap_profiles/")
precor_file = "full_profiles_cc_adj_mean_corr.parquet"
precor_path = dir_path / "orf" / precor_file
precor = pl.read_parquet(precor_path)


sampled = sample_ids(precor)
partitioned = group_by_trt(sampled)
negcons_per_plate = 1
seed = 42
p_values = {
    k: get_p_value(a, b, negcons_per_plate=negcons_per_plate, seed=seed)
    for k, (a, b) in tqdm(partitioned.items())
}
# pval_mat = cp.sort(cp.array(list(p_values.values())), axis=0)
pval_mat = np.array(list(p_values.values()))

# %% Correct p values for multiple tests


from statsmodels.stats.multitest import multipletests

corrected = pl.DataFrame(
    [multipletests(x, is_sorted=True)[1] for x in pval_mat.T.get()],
    schema=precor.select(pl.all().exclude("^Metadata.*$")).columns,
)

# %% Group pvalues
from scipy.stats import combine_pvalues
