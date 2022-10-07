from typing import Union, Optional

import pytest

import scanpy as sc
import cellrank.external as cre
from anndata import AnnData
from cellrank.kernels import ConnectivityKernel
from cellrank.external.kernels._utils import MarkerGenes
from cellrank.kernels._transport_map_kernel import SelfTransitions

import numpy as np
import pandas as pd
from scipy.sparse import issparse, csr_matrix
from pandas.core.dtypes.common import is_categorical_dtype

import matplotlib.pyplot as plt
from matplotlib.cm import get_cmap
from matplotlib.colors import to_hex


def _wot_not_installed() -> bool:
    try:
        import wot

        return False
    except ImportError:
        return True


def _statot_not_installed() -> bool:
    try:
        import statot

        return False
    except ImportError:
        return True


wot_not_installed_skip = pytest.mark.skipif(
    _wot_not_installed(), reason="WOT is not installed."
)
statot_not_installed_skip = pytest.mark.skipif(
    _statot_not_installed(), reason="statOT is not installed."
)


@statot_not_installed_skip
class TestOTKernel:
    def test_method_not_implemented(self, adata_large: AnnData):
        terminal_states = np.full((adata_large.n_obs,), fill_value=np.nan, dtype=object)
        ixs = np.where(adata_large.obs["clusters"] == "Granule immature")[0]
        terminal_states[ixs] = "GI"

        ok = cre.kernels.StationaryOTKernel(
            adata_large,
            terminal_states=pd.Series(terminal_states).astype("category"),
            g=np.ones((adata_large.n_obs,), dtype=np.float64),
        )

        with pytest.raises(
            NotImplementedError, match="Method `'unbal'` is not yet implemented."
        ):
            ok.compute_transition_matrix(1, 0.001, method="unbal")

    def test_no_terminal_states(self, adata_large: AnnData):
        with pytest.raises(RuntimeError, match="Unable to initialize the kernel."):
            cre.kernels.StationaryOTKernel(
                adata_large,
                g=np.ones((adata_large.n_obs,), dtype=np.float64),
            )

    def test_normal_run(self, adata_large: AnnData):
        terminal_states = np.full((adata_large.n_obs,), fill_value=np.nan, dtype=object)
        ixs = np.where(adata_large.obs["clusters"] == "Granule immature")[0]
        terminal_states[ixs] = "GI"

        ok = cre.kernels.StationaryOTKernel(
            adata_large,
            terminal_states=pd.Series(terminal_states).astype("category"),
            g=np.ones((adata_large.n_obs,), dtype=np.float64),
        )
        ok = ok.compute_transition_matrix(1, 0.001)

        assert isinstance(ok, cre.kernels.StationaryOTKernel)
        assert isinstance(ok._transition_matrix, csr_matrix)
        np.testing.assert_allclose(ok.transition_matrix.sum(1), 1.0)
        assert isinstance(ok.params, dict)

    @pytest.mark.parametrize("connectivity_kernel", (None, ConnectivityKernel))
    def test_compute_projection(
        self, adata_large: AnnData, connectivity_kernel: Optional[ConnectivityKernel]
    ):
        terminal_states = np.full((adata_large.n_obs,), fill_value=np.nan, dtype=object)
        ixs = np.where(adata_large.obs["clusters"] == "Granule immature")[0]
        terminal_states[ixs] = "GI"

        ok = cre.kernels.StationaryOTKernel(
            adata_large,
            terminal_states=pd.Series(terminal_states).astype("category"),
            g=np.ones((adata_large.n_obs,), dtype=np.float64),
        )
        ok = ok.compute_transition_matrix(1, 0.001)

        if connectivity_kernel is not None:
            ck = connectivity_kernel(adata_large).compute_transition_matrix()
            combined_kernel = 0.9 * ok + 0.1 * ck
            combined_kernel.compute_transition_matrix()
            combined_kernel.plot_projection()
        else:
            with pytest.raises(RuntimeError, match=r"Unable to find connectivities"):
                ok.plot_projection()


@wot_not_installed_skip
class TestWOTKernel:
    def test_invalid_solver_kwargs(self, adata_large: AnnData):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        with pytest.raises(TypeError, match="unexpected keyword argument 'foo'"):
            ok.compute_transition_matrix(foo="bar")

    def test_inversion_updates_adata(self, adata_large: AnnData):
        key = "age(days)"
        ok = cre.kernels.WOTKernel(adata_large, time_key=key)
        assert is_categorical_dtype(adata_large.obs[key])
        assert adata_large.obs[key].cat.ordered
        np.testing.assert_array_equal(ok.experimental_time, adata_large.obs[key])
        orig_time = ok.experimental_time

        ok = ~ok

        assert ok.transition_matrix is None
        assert ok.transport_maps is None

        inverted_time = ok.experimental_time
        assert is_categorical_dtype(adata_large.obs[key])
        assert adata_large.obs[key].cat.ordered
        np.testing.assert_array_equal(ok.experimental_time, adata_large.obs[key])
        np.testing.assert_array_equal(
            orig_time.cat.categories, inverted_time.cat.categories
        )
        np.testing.assert_array_equal(orig_time.index, inverted_time.index)

        with pytest.raises(AssertionError):
            np.testing.assert_array_equal(orig_time, inverted_time)

    @pytest.mark.parametrize("cmap", ["inferno", "viridis"])
    def test_update_colors(self, adata_large: AnnData, cmap: str):
        ckey = "age(days)_colors"
        _ = cre.kernels.WOTKernel(adata_large, time_key="age(days)", cmap=cmap)

        colors = adata_large.uns[ckey]
        cmap = get_cmap(cmap)

        assert isinstance(colors, np.ndarray)
        assert colors.shape == (2,)
        np.testing.assert_array_equal(colors, [to_hex(cmap(0)), to_hex(cmap(cmap.N))])

    @pytest.mark.parametrize("cmat", [None, "Ms", "X_pca", "good_shape", "bad_shape"])
    def test_cost_matrices(self, adata_large: AnnData, cmat: str):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        if isinstance(cmat, str) and "shape" in cmat:
            cost_matrices = {
                (12.0, 35.0): np.random.normal(size=(73 + ("bad" in cmat), 127))
            }
        else:
            cost_matrices = cmat

        if cmat == "bad_shape":
            with pytest.raises(ValueError, match=r"Expected cost matrix for time pair"):
                ok.compute_transition_matrix(cost_matrices=cost_matrices)
        else:
            ok = ok.compute_transition_matrix(cost_matrices=cost_matrices)
            param = ok.params["cost_matrices"]
            if cmat == "Ms":
                assert param == "Ms"
            elif cmat == "X_pca":
                assert param == "X_pca"
            elif cmat == "good_shape":
                assert param is cost_matrices
            else:
                assert param is None

    @pytest.mark.parametrize("n_iters", [3, 5])
    def test_growth_rates(self, adata_large: AnnData, n_iters: int):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix(growth_iters=n_iters)

        assert isinstance(ok.growth_rates, pd.DataFrame)
        np.testing.assert_array_equal(adata_large.obs_names, ok.growth_rates.index)
        np.testing.assert_array_equal(
            ok.growth_rates.columns, [f"g{i}" for i in range(n_iters + 1)]
        )
        np.testing.assert_array_equal(
            adata_large.obs["estimated_growth_rates"], ok.growth_rates[f"g{n_iters}"]
        )
        assert ok.params["growth_iters"] == n_iters

    @pytest.mark.parametrize("key_added", [None, "gr"])
    def test_birth_death_process(self, adata_large: AnnData, key_added: Optional[str]):
        np.random.seed(42)
        adata_large.obs["foo"] = np.random.normal(size=(adata_large.n_obs,))
        adata_large.obs["bar"] = np.random.normal(size=(adata_large.n_obs,))

        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        gr = ok.compute_initial_growth_rates("foo", "bar", key_added=key_added)

        if key_added is None:
            assert isinstance(gr, pd.Series)
            np.testing.assert_array_equal(gr.index, adata_large.obs_names)
        else:
            assert gr is None
            assert "gr" in adata_large.obs

    @pytest.mark.parametrize("st", list(SelfTransitions))
    def test_self_transitions(self, adata_large: AnnData, st: SelfTransitions):
        et, lt = 12.0, 35.0
        key = "age(days)"
        conn_kwargs = {"n_neighbors": 11}

        ok = cre.kernels.WOTKernel(adata_large, time_key=key).compute_transition_matrix(
            self_transitions=st,
            conn_weight=0.2,
            conn_kwargs=conn_kwargs,
            threshold=None,
        )

        ixs = np.where(adata_large.obs[key] == lt)[0]
        T = ok.transition_matrix[ixs, :][:, ixs].A

        assert ok.params["self_transitions"] == str(st)
        if st == SelfTransitions.UNIFORM:
            np.testing.assert_allclose(T, np.ones_like(T) / float(len(ixs)))
        elif st == SelfTransitions.DIAGONAL:
            np.testing.assert_allclose(T, np.eye(len(ixs)))
        elif st in (SelfTransitions.CONNECTIVITIES, SelfTransitions.ALL):
            adata_subset = adata_large[adata_large.obs[key] == lt]
            sc.pp.neighbors(adata_subset, **conn_kwargs)
            T_actual = (
                ConnectivityKernel(adata_subset)
                .compute_transition_matrix()
                .transition_matrix.A
            )
            np.testing.assert_allclose(T, T_actual)
            if st == SelfTransitions.ALL:
                ixs = np.where(adata_large.obs[key] == et)[0]
                T = ok.transition_matrix[ixs, :][:, ixs].A
                adata_subset = adata_large[adata_large.obs[key] == et]
                sc.pp.neighbors(adata_subset, **conn_kwargs)
                T_actual = (
                    ConnectivityKernel(adata_subset)
                    .compute_transition_matrix()
                    .transition_matrix.A
                )
                np.testing.assert_allclose(T, 0.2 * T_actual)
        else:
            raise NotImplementedError(st)

    @pytest.mark.parametrize("organism", ["human", "mouse"])
    def test_compute_scores_default(self, adata_large: AnnData, organism: str):
        pk, ak = "p_score", "a_score"
        if organism == "human":
            adata_large.var_names = adata_large.var_names.str.upper()

        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        assert pk not in ok.adata.obs
        assert ak not in ok.adata.obs

        ok.compute_initial_growth_rates(
            organism=organism, proliferation_key=pk, apoptosis_key=ak, use_raw=False
        )

        assert pk in ok.adata.obs
        assert ak in ok.adata.obs

    def test_normal_run(self, adata_large: AnnData):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix()

        assert isinstance(ok, cre.kernels.WOTKernel)
        assert isinstance(ok._transition_matrix, csr_matrix)
        np.testing.assert_allclose(ok.transition_matrix.sum(1), 1.0)
        assert isinstance(ok.params, dict)
        assert isinstance(ok.growth_rates, pd.DataFrame)
        assert isinstance(ok.transport_maps, dict)
        for v in ok.transport_maps.values():
            assert isinstance(v, AnnData)
        np.testing.assert_array_equal(adata_large.obs_names, ok.growth_rates.index)
        np.testing.assert_array_equal(ok.growth_rates.columns, ["g0", "g1"])
        assert isinstance(ok.transport_maps[12.0, 35.0], AnnData)
        assert ok.transport_maps[12.0, 35.0].X.dtype == np.float64

    @pytest.mark.parametrize("threshold", [None, 90, 100, "auto"])
    def test_threshold(self, adata_large, threshold: Optional[Union[int, str]]):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix(threshold=threshold)

        assert isinstance(ok._transition_matrix, csr_matrix)
        np.testing.assert_allclose(ok.transition_matrix.sum(1), 1.0)
        assert ok.params["threshold"] == threshold
        if threshold is not None:
            assert issparse(ok.transport_maps[12.0, 35.0].X)

    def test_copy(self, adata_large: AnnData):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix()

        ok2 = ok.copy()

        assert isinstance(ok2, cre.kernels.WOTKernel)
        assert ok is not ok2
        np.testing.assert_array_equal(ok.transition_matrix.A, ok2.transition_matrix.A)

    @pytest.mark.parametrize("connectivity_kernel", (None, ConnectivityKernel))
    def test_compute_projection(
        self, adata_large: AnnData, connectivity_kernel: Optional[ConnectivityKernel]
    ):
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix()

        if connectivity_kernel is not None:
            ck = connectivity_kernel(adata_large).compute_transition_matrix()
            combined_kernel = 0.9 * ok + 0.1 * ck
            combined_kernel.compute_transition_matrix()
            combined_kernel.plot_projection()
        else:
            with pytest.raises(RuntimeError, match=r"Unable to find connectivities"):
                ok.plot_projection()

    def test_wot_write(self, adata_large: AnnData, tmpdir):
        path = str(tmpdir / "wot.pickle")
        ok = cre.kernels.WOTKernel(adata_large, time_key="age(days)")
        ok = ok.compute_transition_matrix()
        ok.write(path)

        ok2 = cre.kernels.WOTKernel.read(path)
        assert ok.params == ok2.params
        np.testing.assert_array_equal(
            sorted(ok.transport_maps.keys()), sorted(ok2.transport_maps.keys())
        )
        np.testing.assert_array_equal(ok.transition_matrix.A, ok2.transition_matrix.A)

    @pytest.mark.parametrize("cmap", ["viridis", "inferno"])
    @pytest.mark.parametrize("size", [5, 10])
    def test_colormap(self, adata_large: AnnData, cmap: str, size: int):
        dummy_time = pd.cut(adata_large.obs["latent_time"], size).cat.rename_categories(
            range(13, 13 + size)
        )
        adata_large.obs["dummy_time"] = dummy_time
        expected_colors = [
            to_hex(c) for c in plt.get_cmap(cmap)(np.linspace(0.0, 1.0, size))
        ]

        _ = cre.kernels.WOTKernel(adata_large, time_key="dummy_time", cmap=cmap)

        np.testing.assert_array_equal(
            adata_large.uns["dummy_time_colors"], expected_colors
        )


class TestGetMarkers:
    @pytest.mark.parametrize("kind", ["proliferation", "apoptosis"])
    @pytest.mark.parametrize("organism", ["human", "mouse", "foo"])
    def test_get_markers(self, organism: str, kind: str):
        if organism == "foo":
            with pytest.raises(NotImplementedError, match=r""):
                getattr(MarkerGenes, f"{kind}_markers")(organism)
        else:
            markers = getattr(MarkerGenes, f"{kind}_markers")(organism)
            assert isinstance(markers, tuple)
            assert np.all([isinstance(marker, str) for marker in markers])
            if organism == "human":
                np.testing.assert_array_equal(
                    markers, [marker.upper() for marker in markers]
                )
