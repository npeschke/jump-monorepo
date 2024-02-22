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
Select the perturbations with highest and lowest feature values
for CRISPR (TODO waiting for data) and ORF datasets using a GPU,
then wrangle information and produce an explorable data frame.

This is intended for use on a server with GPUs and high RAM to analyse
 data massively.

Steps:
- Group feature names using regular expression
- Get median from the grouped subfeatures
- Build dataframe
"""
from pathlib import Path

import cupy as cp
import numpy as np
import polars as pl
from jump_rr.concensus import (
    format_val,
    get_concensus_meta_urls,
    get_cycles,
    repeat_cycles,
)
from jump_rr.index_selection import get_edge_indices
from jump_rr.parse_features import get_feature_groups
from jump_rr.significance import get_corrected_pvals
from jump_rr.translate import get_mappers

assert cp.cuda.get_current_stream().done, "GPU not available"

# %% Setup
## Paths
dir_path = Path("/dgx1nas1/storage/data/shared/morphmap_profiles/")
output_dir = Path("./databases")
precor_file = "full_profiles_cc_adj_mean_corr.parquet"
precor_path = dir_path / "orf" / precor_file

## Parameters
n_vals_used = 50  # Number of top and bottom matches used
plate_type = "orf"

## Column names
jcp_short = "JCP2022"  # Shortened input data frame
jcp_col = f"Metadata_{jcp_short}"  # Traditional JUMP metadata colname
match_col = "Match"  # Highest matches
match_url_col = f"{match_col} Example"  # URL with image examples
std_outname = "Gene/Compound"  # Standard item name
ext_links_col = "Resources"  # Link to external resources (e.g., NCBI)
url_col = "Metadata_image"  # Must start with "Metadata" for URL grouping to work
feature_names = ("Mask", "Feature", "Channel")


# %% Loading
precor = pl.read_parquet(precor_path)

# %% Split data into med (concensus), meta and urls

# Note that we remove the negcons from these analysis, as they are used to
# produce p values on significance.py
med, meta, urls = get_concensus_meta_urls(
    precor.filter(pl.col("Metadata_pert_type") != "negcon")
)
med_vals = med.select(pl.exclude("^Metadata.*$"))
feature_meta = get_feature_groups(tuple(med_vals.columns))

features = pl.concat((feature_meta, med_vals.transpose()), how="horizontal")

feat_med = features.group_by(feature_names).median()


# Find top and bottom $n_values_used

# xs, ys = get_bottom_top_indices(vals, n_vals_used, skip_first=False)
# %%
# Calculate or read likelihood estimates
corrected_pvals = get_corrected_pvals(precor_path)
median_vals = cp.array(feat_med.select(pl.col("^column.*$")).to_numpy())
vals = cp.array(corrected_pvals.drop(feature_names))
xs, ys = get_edge_indices(
    vals,
    n_vals_used,
)

url_vals = urls.get_column(url_col).to_numpy()
cycles = get_cycles(plate_type)
cycled_indices = repeat_cycles(len(xs), plate_type)


# %% Build Data Frame
df = pl.DataFrame(
    {
        **{
            col: np.repeat(feat_med.get_column(col), n_vals_used)
            for col in feature_names
        },
        "Statistic": vals[xs, ys].get(),
        "Median": median_vals[xs, ys].get(),
        jcp_short: med[jcp_col][ys],
        url_col: [  # Use indices to fetch matches
            format_val("img", (img_src, img_src))
            for url, idx in zip(url_vals[ys], cycled_indices[ys])
            if (img_src := next(url).format(next(idx)))
        ],
    }
)

uniq = tuple(df.get_column(jcp_short).unique())
jcp_std_mapper, jcp_external_mapper = get_mappers(uniq, plate_type)

jcp_translated = df.with_columns(
    pl.col(jcp_short).replace(jcp_std_mapper).alias(std_outname),
    pl.col(jcp_short).replace(jcp_external_mapper).alias(ext_links_col),
)

# Reorder columns
order = [
    "Mask",
    "Feature",
    "Channel",
    "Statistic",
    std_outname,
    url_col,
    "Median",
    jcp_short,
]
sorted_df = jcp_translated.select(order)

# Output
output_dir.mkdir(parents=True, exist_ok=True)
sorted_df.write_parquet(output_dir / "orf_features.parquet", compression="zstd")

all_vals = df.select(pl.all().exclude("^Metadata_.*$"))

# Procedure
# 1. Group all features by JCP_ID
# 2. Get the stats for all groups
# 2a. Compare JCPX samples with JCP(Average)
# 3. Use
