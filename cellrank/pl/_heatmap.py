# -*- coding: utf-8 -*-
"""Heatmap module."""

import os
from math import fabs
from typing import Any, Dict, List, Tuple, Union, TypeVar, Optional, Sequence
from pathlib import Path
from collections import Iterable, defaultdict

import numpy as np
import pandas as pd
from pandas.api.types import is_categorical_dtype
from scipy.ndimage.filters import convolve

import matplotlib as mpl
import matplotlib.cm as cm
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from matplotlib.ticker import FormatStrFormatter
from mpl_toolkits.axes_grid1.inset_locator import inset_axes

from cellrank import logging as logg
from cellrank.ul._docs import d, inject_docs
from cellrank.pl._utils import (
    _model_type,
    _get_backend,
    _callback_type,
    _create_models,
    _fit_gene_trends,
    _time_range_type,
    _create_callbacks,
    _maybe_create_dir,
)
from cellrank.tl._utils import save_fig, _min_max_scale, _unique_order_preserving
from cellrank.ul._utils import _get_n_cores, valuedispatch, check_collection
from cellrank.tl._colors import _create_categorical_colors
from cellrank.tl._constants import _DEFAULT_BACKEND, ModeEnum, AbsProbKey
from cellrank.ul._parallelize import parallelize

_N_XTICKS = 10

AnnData = TypeVar("AnnData")
Cmap = TypeVar("Cmap")
Norm = TypeVar("Norm")
Ax = TypeVar("Ax")
Fig = TypeVar("Fig")


class HeatmapMode(ModeEnum):  # noqa
    GENES = "genes"
    LINEAGES = "lineages"


@d.dedent
@inject_docs(m=HeatmapMode)
def heatmap(
    adata: AnnData,
    model: _model_type,
    genes: Sequence[str],
    lineages: Optional[Union[str, Sequence[str]]] = None,
    backward: bool = False,
    mode: str = HeatmapMode.LINEAGES.s,
    time_range: Optional[Union[_time_range_type, List[_time_range_type]]] = None,
    callback: _callback_type = None,
    cluster_key: Optional[Union[str, Sequence[str]]] = None,
    show_absorption_probabilities: bool = False,
    cluster_genes: bool = False,
    keep_gene_order: bool = False,
    scale: bool = True,
    n_convolve: Optional[int] = 5,
    show_all_genes: bool = False,
    show_cbar: bool = True,
    lineage_height: float = 0.1,
    fontsize: Optional[float] = None,
    xlabel: Optional[str] = None,
    cmap: mcolors.ListedColormap = cm.viridis,
    show_dendrogram: bool = True,
    return_genes: bool = False,
    n_jobs: Optional[int] = 1,
    backend: str = _DEFAULT_BACKEND,
    show_progress_bar: bool = True,
    ext: str = "png",
    figsize: Optional[Tuple[float, float]] = None,
    dpi: Optional[int] = None,
    save: Optional[Union[str, Path]] = None,
    **kwargs,
) -> Optional[Dict[str, pd.DataFrame]]:
    """
    Plot a heatmap of smoothed gene expression along specified lineages.

    .. image:: https://raw.githubusercontent.com/theislab/cellrank/master/resources/images/heatmap.png
       :width: 400px
       :align: center

    Parameters
    ----------
    %(adata)s
    %(model)s
    genes
        Genes in :paramref:`adata` `.var_names` to plot or in :paramref:`adata` `.raw.var_names`, if `use_raw=True`.
    lineages
        Names of the lineages which to plot. If `None`, plot all lineages.
    %(backward)s
    mode
        Valid options are:

            - `{m.LINEAGES.s!r}` - group by :paramref:`genes` for each lineage in :paramref:`lineage_names`
            - `{m.GENES.s!r}` - group by :paramref:`lineage_names` for each gene in :paramref:`genes`
    %(time_ranges)s
    %(model_callback)s
    cluster_key
        Key(s) in :paramref:`adata: :.obs` containing categorical observations to be plotted on the top
        of the heatmap. Only available when :paramref:`kind` `='lineages'`.
    show_absorption_probabilities
        Whether to also plot absorption probabilities alongside the smoothed expression.
    cluster_genes
        Whether to use :func:`seaborn.clustermap` when :paramref:`kind` `='lineages'`.
    keep_gene_order
        Whether to keep the gene order for later lineages after the first was sorted.
        Only available when :paramref:`cluster_genes` `=False` and :paramref:`kind` `='lineages'`.
    scale
        Whether to scale the expression per gene to `0-1` range.
    n_convolve
        Size of the convolution window when smoothing out absorption probabilities.
    show_all_genes
        Whether to show all genes on y-axis.
    show_cbar
        Whether to show the colorbar.
    lineage_height
        Height of a bar when :paramref:`kind` ='lineages'.
    fontsize
        Size of the title's font.
    xlabel
        Label on the x-axis. If `None`, it is determined based on :paramref:`time_key`.
    cmap
        Colormap to use when visualizing the smoothed expression.
    show_dendrogram
        Whether to show dendrogram when :paramref:`` `=True`.
    return_genes
        Whether to return the sorted or clustered genes.
    %(parallel)s
    %(plotting)s
    **kwargs
        Keyword arguments for :meth:`cellrank.ul.models.BaseModel.prepare`.

    Returns
    -------
    :class:`pandas.DataFrame`
        If :paramref`return_genes`, returns :class:`pandas.DataFrame`, containing the clustered or sorted genes.
    %(just_plots)s
    """

    import seaborn as sns

    def find_indices(series: pd.Series, values) -> Tuple[Any]:
        def find_nearest(array: np.ndarray, value: float) -> int:
            ix = np.searchsorted(array, value, side="left")
            if ix > 0 and (
                ix == len(array)
                or fabs(value - array[ix - 1]) < fabs(value - array[ix])
            ):
                return ix - 1
            return ix

        series = series[np.argsort(series.values)]

        return tuple(series[[find_nearest(series.values, v) for v in values]].index)

    def subset_lineage(lname: str, rng: np.ndarray) -> np.ndarray:
        time_series = adata.obs[kwargs.get("time_key", "latent_time")]
        ixs = find_indices(time_series, rng)

        lin = adata[ixs, :].obsm[lineage_key][lname]

        lin = lin.X.copy().squeeze()
        if n_convolve is not None:
            lin = convolve(lin, np.ones(n_convolve) / n_convolve, mode="nearest")

        return lin

    def create_col_colors(lname: str, rng: np.ndarray) -> Tuple[np.ndarray, Cmap, Norm]:
        color = adata.obsm[lineage_key][lname].colors[0]
        lin = subset_lineage(lname, rng)

        h, _, v = mcolors.rgb_to_hsv(mcolors.to_rgb(color))
        end_color = mcolors.hsv_to_rgb([h, 1, v])

        lineage_cmap = mcolors.LinearSegmentedColormap.from_list(
            "lineage_cmap", ["#ffffff", end_color], N=len(rng)
        )
        norm = mcolors.Normalize(vmin=np.min(lin), vmax=np.max(lin))
        scalar_map = cm.ScalarMappable(cmap=lineage_cmap, norm=norm)

        return (
            np.array([mcolors.to_hex(c) for c in scalar_map.to_rgba(lin)]),
            lineage_cmap,
            norm,
        )

    def create_col_categorical_color(cluster_key: str, rng: np.ndarray) -> np.ndarray:
        if not is_categorical_dtype(adata.obs[cluster_key]):
            raise TypeError(
                f"Expected `adata.obs[{cluster_key!r}]` to be categorical, "
                f"found `{adata.obs[cluster_key].dtype.name!r}`."
            )

        color_key = f"{cluster_key}_colors"
        if color_key not in adata.uns:
            logg.warning(
                f"Color key `{color_key!r}` not found in `adata.uns`. Creating new colors"
            )
            colors = _create_categorical_colors(
                len(adata.obs[cluster_key].cat.categories)
            )
            adata.uns[color_key] = colors
        else:
            colors = adata.uns[color_key]

        time_series = adata.obs[kwargs.get("time_key", "latent_time")]
        ixs = find_indices(time_series, rng)

        mapper = dict(zip(adata.obs[cluster_key].cat.categories, colors))

        return np.array(
            [mcolors.to_hex(mapper[v]) for v in adata[ixs, :].obs[cluster_key].values]
        )

    def create_cbar(ax, x_delta: float, cmap, norm, label=None) -> Ax:
        cax = inset_axes(
            ax,
            width="1%",
            height="100%",
            loc="lower right",
            bbox_to_anchor=(x_delta, 0, 1, 1),
            bbox_transform=ax.transAxes,
        )

        _ = mpl.colorbar.ColorbarBase(cax, cmap=cmap, norm=norm, label=label)

        return cax

    @valuedispatch
    def _plot_heatmap(_mode: HeatmapMode) -> Fig:
        pass

    @_plot_heatmap.register(HeatmapMode.GENES)
    def _() -> Fig:
        def color_fill_rec(ax, xs, y1, y2, colors=None, cmap=cmap, **kwargs) -> None:
            colors = colors if cmap is None else cmap(colors)

            x = 0
            for i, (color, x, y1, y2) in enumerate(zip(colors, xs, y1, y2)):
                dx = (xs[i + 1] - xs[i]) if i < len(x) else (xs[-1] - xs[-2])
                ax.add_patch(
                    plt.Rectangle((x, y1), dx, y2 - y1, color=color, ec=color, **kwargs)
                )

            ax.plot(x, y2, lw=0)

        fig, axes = plt.subplots(
            nrows=len(genes) + show_absorption_probabilities,
            figsize=(15, len(genes) + len(lineages)) if figsize is None else figsize,
            dpi=dpi,
        )

        if not isinstance(axes, Iterable):
            axes = [axes]
        axes = np.ravel(axes)

        if show_absorption_probabilities:
            data["absorption probability"] = data[next(iter(data.keys()))]

        for ax, (gene, models) in zip(axes, data.items()):
            if scale:
                norm = mcolors.Normalize(vmin=0, vmax=1)
            else:
                c = np.array([m.y_test for m in models.values()])
                c_min, c_max = np.nanmin(c), np.nanmax(c)
                norm = mcolors.Normalize(vmin=c_min, vmax=c_max)

            ix = 0
            ys = [ix]

            if gene == "absorption probability":
                norm = mcolors.Normalize(vmin=0, vmax=1)
                for ln, x in ((ln, m.x_test) for ln, m in models.items()):
                    y = np.ones_like(x)
                    c = subset_lineage(ln, x.squeeze())

                    color_fill_rec(
                        ax, x, y * ix, y * (ix + lineage_height), colors=norm(c)
                    )

                    ix += lineage_height
                    ys.append(ix)
            else:
                for x, c in ((m.x_test, m.y_test) for m in models.values()):
                    y = np.ones_like(x)
                    c = _min_max_scale(c) if scale else c

                    color_fill_rec(
                        ax, x, y * ix, y * (ix + lineage_height), colors=norm(c)
                    )

                    ix += lineage_height
                    ys.append(ix)

            xs = np.array([m.x_test for m in models.values()])
            x_min, x_max = np.min(xs), np.max(xs)
            ax.set_xticks(np.linspace(x_min, x_max, _N_XTICKS))

            ax.set_yticks(np.array(ys[:-1]) + lineage_height / 2)
            ax.spines["left"].set_position(
                ("data", 0)
            )  # move the left spine to the rectangles to get nicer yticks
            ax.set_yticklabels(lineages, ha="right")

            ax.set_title(gene, fontdict=dict(fontsize=fontsize))
            ax.set_ylabel("lineage")

            for pos in ["top", "bottom", "left", "right"]:
                ax.spines[pos].set_visible(False)

            cax, _ = mpl.colorbar.make_axes(ax)
            _ = mpl.colorbar.ColorbarBase(
                cax,
                norm=norm,
                cmap=cmap,
                label="value" if gene == "absorption probability" else "expression",
            )

            ax.tick_params(
                top=False,
                bottom=False,
                left=True,
                right=False,
                labelleft=True,
                labelbottom=False,
            )

        ax.xaxis.set_major_formatter(FormatStrFormatter("%.3f"))
        ax.tick_params(
            top=False,
            bottom=True,
            left=True,
            right=False,
            labelleft=True,
            labelbottom=True,
        )
        ax.set_xlabel(xlabel)

        return fig

    @_plot_heatmap.register(HeatmapMode.LINEAGES)
    def _() -> Tuple[List[Fig], pd.DataFrame]:
        data_t = defaultdict(dict)  # transpose
        for gene, lns in data.items():
            for ln, y in lns.items():
                data_t[ln][gene] = y

        figs = []
        gene_order = None
        sorted_genes = pd.DataFrame() if return_genes else None

        for lname, models in data_t.items():
            xs = np.array([m.x_test for m in models.values()])
            x_min, x_max = np.nanmin(xs), np.nanmax(xs)

            df = pd.DataFrame([m.y_test for m in models.values()], index=genes)
            df.index.name = "genes"

            if not cluster_genes:
                if gene_order is not None:
                    df = df.loc[gene_order]
                else:
                    max_sort = np.argsort(
                        np.argmax(df.apply(_min_max_scale, axis=1).values, axis=1)
                    )
                    df = df.iloc[max_sort, :]
                    if keep_gene_order:
                        gene_order = df.index

            cat_colors = None
            if cluster_key is not None:
                cat_colors = np.stack(
                    [
                        create_col_categorical_color(
                            c, np.linspace(x_min, x_max, df.shape[1])
                        )
                        for c in cluster_key
                    ],
                    axis=0,
                )

            if show_absorption_probabilities:
                col_colors, col_cmap, col_norm = create_col_colors(
                    lname, np.linspace(x_min, x_max, df.shape[1])
                )
                if cat_colors is not None:
                    col_colors = np.vstack([cat_colors, col_colors[None, :]])
            else:
                col_colors, col_cmap, col_norm = cat_colors, None, None

            row_cluster = cluster_genes and df.shape[0] > 1
            show_clust = row_cluster and show_dendrogram

            g = sns.clustermap(
                df,
                cmap=cmap,
                figsize=(10, min(len(genes) / 8 + 1, 10))
                if figsize is None
                else figsize,
                xticklabels=False,
                cbar_kws={"label": "expression"},
                row_cluster=cluster_genes and df.shape[0] > 1,
                col_colors=col_colors,
                colors_ratio=0,
                col_cluster=False,
                cbar_pos=None,
                yticklabels=show_all_genes or "auto",
                standard_scale=0 if scale else None,
            )

            if show_cbar:
                cax = create_cbar(
                    g.ax_heatmap,
                    0.1,
                    cmap=cmap,
                    norm=mcolors.Normalize(
                        vmin=0 if scale else np.min(df.values),
                        vmax=1 if scale else np.max(df.values),
                    ),
                    label="expression",
                )
                g.fig.add_axes(cax)

                if col_cmap is not None and col_norm is not None:
                    cax = create_cbar(
                        g.ax_heatmap,
                        0.25,
                        cmap=col_cmap,
                        norm=col_norm,
                        label="absorption probability",
                    )
                    g.fig.add_axes(cax)

            if g.ax_col_colors:
                main_bbox = _get_ax_bbox(g.fig, g.ax_heatmap)
                n_bars = show_absorption_probabilities + (
                    len(cluster_key) if cluster_key is not None else 0
                )
                _set_ax_height_to_cm(
                    g.fig,
                    g.ax_col_colors,
                    height=min(
                        5, max(n_bars * main_bbox.height / len(df), 0.25 * n_bars)
                    ),
                )
                g.ax_col_colors.set_title(lname, fontdict=dict(fontsize=fontsize))
            else:
                g.ax_heatmap.set_title(lname, fontdict=dict(fontsize=fontsize))

            g.ax_col_dendrogram.set_visible(False)  # gets rid of top free space

            g.ax_heatmap.yaxis.tick_left()
            g.ax_heatmap.yaxis.set_label_position("left")

            g.ax_heatmap.set_xlabel(xlabel)
            g.ax_heatmap.set_xticks(np.linspace(0, len(df.columns), _N_XTICKS))
            g.ax_heatmap.set_xticklabels(
                list(map(lambda n: round(n, 3), np.linspace(x_min, x_max, _N_XTICKS)))
            )

            if show_clust:
                # robustly show dendrogram, because gene names can be long
                g.ax_row_dendrogram.set_visible(True)
                dendro_box = g.ax_row_dendrogram.get_position()

                pad = 0.005
                bb = g.ax_heatmap.yaxis.get_tightbbox(
                    g.fig.canvas.get_renderer()
                ).transformed(g.fig.transFigure.inverted())

                dendro_box.x0 = bb.x0 - dendro_box.width - pad
                dendro_box.x1 = bb.x0 - pad

                g.ax_row_dendrogram.set_position(dendro_box)
            else:
                g.ax_row_dendrogram.set_visible(False)

            if return_genes:
                sorted_genes[lname] = (
                    df.index[g.dendrogram_row.reordered_ind]
                    if hasattr(g, "dendrogram_row") and g.dendrogram_row is not None
                    else df.index
                )

            figs.append(g)

        return figs, sorted_genes

    mode = HeatmapMode(mode)

    lineage_key = str(AbsProbKey.BACKWARD if backward else AbsProbKey.FORWARD)
    if lineage_key not in adata.obsm:
        raise KeyError(f"Lineages key `{lineage_key!r}` not found in `adata.obsm`.")

    if lineages is None:
        lineages = adata.obsm[lineage_key].names
    elif isinstance(lineages, str):
        lineages = [lineages]
    lineages = _unique_order_preserving(lineages)

    _ = adata.obsm[lineage_key][lineages]

    if cluster_key is not None:
        if isinstance(cluster_key, str):
            cluster_key = [cluster_key]
        cluster_key = _unique_order_preserving(cluster_key)

    if isinstance(genes, str):
        genes = [genes]
    genes = _unique_order_preserving(genes)
    check_collection(adata, genes, "var_names", use_raw=kwargs.get("use_raw", False))

    if isinstance(time_range, (tuple, float, int, type(None))):
        time_range = [time_range] * len(lineages)
    elif len(time_range) != len(lineages):
        raise ValueError(
            f"Expected time ranges to be of length `{len(lineages)}`, found `{len(time_range)}`."
        )

    xlabel = xlabel or kwargs.get("time_key", "latent_time")
    _ = kwargs.pop("time_range", None)

    models = _create_models(model, genes, lineages)
    callbacks = _create_callbacks(adata, callback, genes, lineages)

    backend = _get_backend(model, backend)
    n_jobs = _get_n_cores(n_jobs, len(genes))

    start = logg.info(f"Computing trends using `{n_jobs}` core(s)")
    data = parallelize(
        _fit_gene_trends,
        genes,
        unit="gene",
        backend=backend,
        n_jobs=n_jobs,
        extractor=lambda data: {k: v for d in data for k, v in d.items()},
        show_progress_bar=show_progress_bar,
    )(models, callbacks, lineages, time_range, **kwargs)

    logg.info("    Finish", time=start)
    logg.debug(f"Plotting `{mode.s!r}` heatmap")

    fig, genes = _plot_heatmap(mode)

    if save is not None and fig is not None:
        if not isinstance(fig, Iterable):
            save_fig(fig, save, ext=ext)
            return
        if len(fig) == 1:
            save_fig(fig[0], save, ext=ext)
            return

        _maybe_create_dir(save)
        for ln, f in zip(lineages, fig):
            save_fig(f, os.path.join(save, f"lineage_{ln}"), ext=ext)

    if return_genes:
        return genes


def _get_ax_bbox(fig: Fig, ax: Ax):
    return ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())


def _set_ax_height_to_cm(fig: Fig, ax: Ax, height: float) -> None:
    from mpl_toolkits.axes_grid1 import Size, Divider

    height /= 2.54  # cm to inches

    bbox = _get_ax_bbox(fig, ax)

    hori = [Size.Fixed(bbox.x0), Size.Fixed(bbox.width), Size.Fixed(bbox.x1)]
    vert = [Size.Fixed(bbox.y0), Size.Fixed(height), Size.Fixed(bbox.y1)]

    divider = Divider(fig, (0.0, 0.0, 1.0, 1.0), hori, vert, aspect=False)

    ax.set_axes_locator(divider.new_locator(nx=1, ny=1))
