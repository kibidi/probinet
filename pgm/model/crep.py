"""
Class definition of CRep, the algorithm to perform inference in networks with reciprocity.
The latent variables are related to community memberships and reciprocity value.
"""
import logging
from pathlib import Path
import time
from typing import Any, List, Optional, Tuple, Union

from numpy import dtype, ndarray
import numpy as np
import sktensor as skt
from typing_extensions import Unpack

from ..input.preprocessing import preprocess
from ..input.tools import (
    get_item_array_from_subs, log_and_raise_error, sp_uttkrp, sp_uttkrp_assortative)
from ..output.evaluate import _lambda0_full
from .base import FitParams, ModelClass


class CRep(ModelClass):
    """
    Class to perform inference in networks with reciprocity.
    """

    def __init__(self,
                 inf: float = 1e10,  # initial value of the pseudo log-likelihood, aka, infinity
                 err_max: float = 1e-12,  # minimum value for the parameters
                 err: float = 0.1,  # noise for the initialization
                 num_realizations: int = 5,  # number of iterations with different random init
                 convergence_tol: float = 1e-4,  # convergence_tol parameter for convergence
                 decision: int = 10,  # convergence parameter
                 max_iter: int = 1000,  # maximum number of EM steps before aborting
                 flag_conv: str = 'log',  # flag to choose the convergence criterion
                 ) -> None:
        super().__init__(
            inf,
            err_max,
            err,
            num_realizations,
            convergence_tol,
            decision,
            max_iter,
            flag_conv)

    def check_fit_params(self,
                         initialization: int,
                         eta0: Union[float, None],
                         undirected: bool,
                         assortative: bool,
                         data: Union[skt.dtensor, skt.sptensor],
                         K: int,
                         constrained: bool,
                         **extra_params: Unpack[FitParams]) -> None:

        message = 'The initialization parameter can be either 0, 1, 2 or 3.'
        available_extra_params = [
            'fix_eta',
            'fix_w',
            'fix_communities',
            'files',
            'out_inference',
            'out_folder',
            'end_file'
        ]
        super()._check_fit_params(
            initialization,
            undirected,
            assortative,
            data,
            K,
            available_extra_params,
            data_X=None,
            gamma=None,
            eta0=eta0,
            message=message,
            **extra_params)

        self.constrained = constrained

        # Parameters for the initialization of the model
        self.use_unit_uniform = True
        self.normalize_rows = True

        if self.initialization > 0:
            self.theta = np.load(Path(self.files).resolve(), allow_pickle=True)

    def fit(self,
            data: Union[skt.dtensor,
                        skt.sptensor],
            data_T: skt.sptensor,
            data_T_vals: np.ndarray,
            nodes: List[Any],
            rseed: int = 0,
            K: int = 3,
            mask: Optional[np.ndarray] = None,
            initialization: int = 0,
            eta0: Union[float,
                        None] = None,
            undirected: bool = False,
            assortative: bool = True,
            constrained: bool = True,
            **extra_params: Unpack[FitParams]) -> tuple[ndarray[Any,
                                                                dtype[np.float64]],
                                                        ndarray[Any,
                                                                dtype[np.float64]],
                                                        ndarray[Any,
                                                                dtype[np.float64]],
                                                        float,
                                                        float]:
        """
        Model directed networks by using a probabilistic generative model that assume community
        parameters and reciprocity coefficient. The inference is performed via EM algorithm.

        Parameters
        ----------
        data : ndarray/sptensor
               Graph adjacency tensor.
        data_T: None/sptensor
                Graph adjacency tensor (transpose) - if sptensor.
        data_T_vals : None/ndarray
                      Array with values of entries A[j, i] given non-zero entry (i, j) - if
                       ndarray.
        nodes : list
                List of nodes IDs.
        flag_conv : str
                    If 'log' the convergence is based on the log-likelihood values; if 'deltas'
                     convergence is based on the differences in the parameters values. The
                     latter is suggested when the dataset is big (N > 1000 ca.).
        mask : ndarray
               Mask for selecting the held out set in the adjacency tensor in case of
               cross-validation.

        Returns
        -------
        u_f : ndarray
              Out-going membership matrix.
        v_f : ndarray
              In-coming membership matrix.
        w_f : ndarray
              Affinity tensor.
        eta_f : float
                Reciprocity coefficient.
        maxL : float
               Maximum pseudo log-likelihood.
        """
        self.check_fit_params(data=data,
                              K=K,
                              initialization=initialization,
                              eta0=eta0,
                              undirected=undirected,
                              assortative=assortative,
                              constrained=constrained,
                              **extra_params)
        logging.debug('Fixing random seed to: %s', rseed)
        self.rng = np.random.RandomState(rseed)  # pylint: disable=no-member
        self.initialization = initialization
        maxL = -self.inf  # initialization of the maximum pseudo log-likelihood
        self.nodes = nodes

        if data_T is None:
            E = np.sum(
                data
            )  # weighted sum of edges (needed in the denominator of eta)
            data_T = np.einsum('aij->aji', data)
            data_T_vals = get_item_array_from_subs(data_T, data.nonzero())
            # pre-processing of the data to handle the sparsity
            data = preprocess(data)
            data_T = preprocess(data_T)
        else:
            E = np.sum(data.vals)

        # save the indexes of the nonzero entries
        if isinstance(data, skt.dtensor):
            subs_nz = data.nonzero()
        elif isinstance(data, skt.sptensor):
            subs_nz = data.subs

        # The following part of the code is responsible for running the Expectation-Maximization (EM) algorithm for a
        # specified number of realizations (self.num_realizations):
        for r in range(self.num_realizations):

            # For each realization (r), it initializes the parameters, updates the old variables
            # and updates the cache.
            logging.debug('Random number generator seed: %s', self.rng.get_state()[1][0])
            self._initialize()
            super()._update_old_variables()
            self._update_cache(data, data_T_vals, subs_nz)

            # It sets up local variables for convergence checking. coincide and it are counters, convergence is a
            # boolean flag, and loglik is the initial pseudo log-likelihood.
            coincide, it = 0, 0
            convergence = False
            loglik = self.inf

            logging.debug('Updating realization %s ...', r)
            time_start = time.time()
            # It enters a while loop that continues until either convergence is achieved or the maximum number of
            # iterations (self.max_iter) is reached.
            while np.logical_and(not convergence, it < self.max_iter):
                #  it performs the main EM update (self._update_em(data, data_T_vals, subs_nz, denominator=E))
                # which updates the memberships and calculates the maximum difference
                # between new and old parameters.
                delta_u, delta_v, delta_w, delta_eta = self._update_em(
                    data, data_T_vals, subs_nz, denominator=E)

                # Depending on the convergence flag (self.flag_conv), it checks for convergence using either the
                # pseudo log-likelihood values (self._check_for_convergence(data, it, loglik, coincide, convergence,
                # data_T=data_T, mask=mask)) or the maximum distances between the old and the new parameters
                # (self._check_for_convergence_delta(it, coincide, delta_u, delta_v, delta_w, delta_eta, convergence)).
                if self.flag_conv == 'log':
                    it, loglik, coincide, convergence = super()._check_for_convergence(
                        data,
                        it,
                        loglik,
                        coincide,
                        convergence,
                        use_pseudo_likelihood=True,
                        data_T=data_T,
                        mask=mask)

                    if not it % 100:
                        logging.debug(
                            'Nreal = {} - Pseudo Log-likelihood = {} - iterations = {} - time = {:.2f} seconds'.format(
                                r, loglik, it, time.time() - time_start))
                elif self.flag_conv == 'deltas':
                    it, coincide, convergence = super()._check_for_convergence_delta(
                        it,
                        coincide,
                        delta_u,
                        delta_v,
                        delta_w,
                        delta_eta,
                        convergence)

                    if not it % 100:
                        logging.debug(
                            'Nreal = {} - iterations = {} - time = {:.2f} seconds'.format(
                                r, it, time.time() - time_start
                            )
                        )
                else:
                    log_and_raise_error(ValueError, 'flag_conv can be either log or deltas!')
            # After the while loop, it checks if the current pseudo log-likelihood is the maximum so far. If it is,
            # it updates the optimal parameters (self._update_optimal_parameters()) and sets maxL to the current
            # pseudo log-likelihood.
            if self.flag_conv == 'log':
                if maxL < loglik:
                    super()._update_optimal_parameters()
                    maxL = loglik
                    self.final_it = it
                    conv = convergence
            elif self.flag_conv == 'deltas':
                loglik = self._PSLikelihood(data, data_T=data_T, mask=mask)
                if maxL < loglik:
                    super()._update_optimal_parameters()
                    maxL = loglik
                    self.final_it = it
                    conv = convergence

            logging.debug(
                'Nreal = %s - Pseudo Log-likelihood = %s - iterations = %s - time = %.2f seconds',
                r, loglik, it, time.time() - time_start
            )

            # end cycle over realizations

        self.maxPSL = maxL

        if np.logical_and(self.final_it == self.max_iter, not conv):
            # convergence not reached
            logging.error('Solution failed to converge in %s EM steps!', self.max_iter)
        if self.out_inference:
            super()._output_results()

        return self.u_f, self.v_f, self.w_f, self.eta_f, maxL

    def _update_cache(
            self,
            data: Union[skt.dtensor, skt.sptensor],
            data_T_vals: np.ndarray,
            subs_nz: Tuple[np.ndarray]) -> None:
        """
        Update the cache used in the em_update.

        Parameters
        ----------
        data : sptensor/dtensor
               Graph adjacency tensor.
        data_T_vals : ndarray
                      Array with values of entries A[j, i] given non-zero entry (i, j).
        subs_nz : tuple
                  Indices of elements of data that are non-zero.
        """

        self.lambda0_nz = super()._lambda_nz(subs_nz)
        self.M_nz = self.lambda0_nz + self.eta * data_T_vals
        self.M_nz[self.M_nz == 0] = 1
        if isinstance(data, skt.dtensor):
            self.data_M_nz = data[subs_nz] / self.M_nz
        elif isinstance(data, skt.sptensor):
            self.data_M_nz = data.vals / self.M_nz
        self.data_M_nz[self.M_nz == 0] = 0

    def _update_em(
            self,
            data: Union[skt.dtensor, skt.sptensor],
            data_T_vals: np.ndarray, subs_nz: Tuple[np.ndarray],
            denominator: float) -> Tuple[float, float, float, float]:
        """
        Update parameters via EM procedure.

        Parameters
        ----------
        data : sptensor/dtensor
               Graph adjacency tensor.
        data_T_vals : ndarray
                      Array with values of entries A[j, i] given non-zero entry (i, j).
        subs_nz : tuple
                  Indices of elements of data that are non-zero.
        denominator : float
                      Denominator used in the update of the eta parameter.

        Returns
        -------
        d_u : float
              Maximum distance between the old and the new membership matrix u.
        d_v : float
              Maximum distance between the old and the new membership matrix v.
        d_w : float
              Maximum distance between the old and the new affinity tensor w.
        d_eta : float
                Maximum distance between the old and the new reciprocity coefficient eta.
        """

        if not self.fix_eta:
            d_eta = self._update_eta(data,
                                     data_T_vals,
                                     denominator=denominator)
        else:
            d_eta = 0.
        self._update_cache(data, data_T_vals, subs_nz)

        if not self.fix_communities:
            d_u = self._update_U(subs_nz)
            self._update_cache(data, data_T_vals, subs_nz)
        else:
            d_u = 0.

        if self.undirected:
            self.v = self.u
            self.v_old = self.v
            d_v = d_u
            self._update_cache(data, data_T_vals, subs_nz)
        else:
            if not self.fix_communities:
                d_v = self._update_V(subs_nz)
                self._update_cache(data, data_T_vals, subs_nz)
            else:
                d_v = 0.

        if not self.fix_w:
            if not self.assortative:
                d_w = self._update_W(subs_nz)
            else:
                d_w = self._update_W_assortative(subs_nz)
            self._update_cache(data, data_T_vals, subs_nz)
        else:
            d_w = 0

        return d_u, d_v, d_w, d_eta

    def _update_eta(self, data: Union[skt.dtensor, skt.sptensor],
                    data_T_vals: np.ndarray,
                    denominator: Optional[float] = None) -> float:
        """
        Update reciprocity coefficient eta.

        Parameters
        ----------
        data : sptensor/dtensor
               Graph adjacency tensor.
        data_T_vals : ndarray
                      Array with values of entries A[j, i] given non-zero entry (i, j).
        denominator : float
                      Denominator used in the update of the eta parameter.

        Returns
        -------
        dist_eta : float
                   Maximum distance between the old and the new reciprocity coefficient eta.
        """

        if denominator is None:
            Deta = data.sum()
        else:
            Deta = denominator

        self.eta *= (self.data_M_nz * data_T_vals).sum() / Deta

        dist_eta = abs(self.eta - self.eta_old)
        self.eta_old = float(self.eta)

        return dist_eta

    def _update_U(self, subs_nz: Tuple[np.ndarray]) -> float:
        """
        Update out-going membership matrix.

        Parameters
        ----------
        subs_nz : tuple
                  Indices of elements of data that are non-zero.

        Returns
        -------
        dist_u : float
                 Maximum distance between the old and the new membership matrix u.
        """

        self.u = self.u_old * self._update_membership(subs_nz, 1)

        if not self.constrained:
            Du = np.einsum('iq->q', self.v)
            if not self.assortative:
                w_k = np.einsum('akq->kq', self.w)
                Z_uk = np.einsum('q,kq->k', Du, w_k)
            else:
                w_k = np.einsum('ak->k', self.w)
                Z_uk = np.einsum('k,k->k', Du, w_k)
            non_zeros = Z_uk > 0.
            self.u[:, Z_uk == 0] = 0.
            self.u[:, non_zeros] /= Z_uk[np.newaxis, non_zeros]
        else:
            row_sums = self.u.sum(axis=1)
            self.u[row_sums > 0] /= row_sums[row_sums > 0, np.newaxis]

        low_values_indices = self.u < self.err_max  # values are too low
        self.u[low_values_indices] = 0.  # and set to 0.

        dist_u = np.amax(abs(self.u - self.u_old))
        self.u_old = np.copy(self.u)

        return dist_u

    def _update_V(self, subs_nz: Tuple[np.ndarray]) -> float:
        """
        Update in-coming membership matrix.
        Same as _update_U but with:
        data <-> data_T
        w <-> w_T
        u <-> v

        Parameters
        ----------
        subs_nz : tuple
                  Indices of elements of data that are non-zero.

        Returns
        -------
        dist_v : float
                 Maximum distance between the old and the new membership matrix v.
        """

        self.v *= self._update_membership(subs_nz, 2)

        if not self.constrained:
            Dv = np.einsum('iq->q', self.u)
            if not self.assortative:
                w_k = np.einsum('aqk->qk', self.w)
                Z_vk = np.einsum('q,qk->k', Dv, w_k)
            else:
                w_k = np.einsum('ak->k', self.w)
                Z_vk = np.einsum('k,k->k', Dv, w_k)
            non_zeros = Z_vk > 0
            self.v[:, Z_vk == 0] = 0.
            self.v[:, non_zeros] /= Z_vk[np.newaxis, non_zeros]
        else:
            row_sums = self.v.sum(axis=1)
            self.v[row_sums > 0] /= row_sums[row_sums > 0, np.newaxis]

        low_values_indices = self.v < self.err_max  # values are too low
        self.v[low_values_indices] = 0.  # and set to 0.

        dist_v = np.amax(abs(self.v - self.v_old))
        self.v_old = np.copy(self.v)

        return dist_v

    def _update_W(self, subs_nz: Tuple[np.ndarray]) -> float:
        """
        Update affinity tensor.

        Parameters
        ----------
        subs_nz : tuple
                  Indices of elements of data that are non-zero.

        Returns
        -------
        dist_w : float
                 Maximum distance between the old and the new affinity tensor w.
        """
        if len(subs_nz) < 3:
            log_and_raise_error(ValueError, "subs_nz should have at least 3 elements.")

        uttkrp_DKQ = np.zeros_like(self.w)

        UV = np.einsum('Ik,Iq->Ikq', self.u[subs_nz[1], :],
                       self.v[subs_nz[2], :])
        uttkrp_I = self.data_M_nz[:, np.newaxis, np.newaxis] * UV
        for k in range(self.K):
            for q in range(self.K):
                uttkrp_DKQ[:, k, q] += np.bincount(subs_nz[0],
                                                   weights=uttkrp_I[:, k, q],
                                                   minlength=self.L)

        self.w *= uttkrp_DKQ

        Z = np.einsum('k,q->kq', self.u.sum(axis=0),
                      self.v.sum(axis=0))[np.newaxis, :, :]
        non_zeros = Z > 0
        self.w[non_zeros] /= Z[non_zeros]

        low_values_indices = self.w < self.err_max  # values are too low
        self.w[low_values_indices] = 0.  # and set to 0.

        dist_w = np.amax(abs(self.w - self.w_old))
        self.w_old = np.copy(self.w)

        return dist_w

    def _update_W_assortative(self, subs_nz: Tuple[np.ndarray]) -> float:
        """
        Update affinity tensor (assuming assortativity).

        Parameters
        ----------
        subs_nz : tuple
                  Indices of elements of data that are non-zero.

        Returns
        -------
        dist_w : float
                 Maximum distance between the old and the new affinity tensor w.
        """
        if len(subs_nz) < 3:
            log_and_raise_error(ValueError, "subs_nz should have at least 3 elements.")

        uttkrp_DKQ = np.zeros_like(self.w)

        UV = np.einsum('Ik,Ik->Ik', self.u[subs_nz[1], :],
                       self.v[subs_nz[2], :])
        uttkrp_I = self.data_M_nz[:, np.newaxis] * UV
        for k in range(self.K):
            uttkrp_DKQ[:, k] += np.bincount(subs_nz[0],
                                            weights=uttkrp_I[:, k],
                                            minlength=self.L)

        self.w *= uttkrp_DKQ

        Z = ((self.u_old.sum(axis=0)) *
             (self.v_old.sum(axis=0)))[np.newaxis, :]
        non_zeros = Z > 0
        self.w[non_zeros] /= Z[non_zeros]

        low_values_indices = self.w < self.err_max  # values are too low
        self.w[low_values_indices] = 0.  # and set to 0.

        dist_w = np.amax(abs(self.w - self.w_old))
        self.w_old = np.copy(self.w)

        return dist_w

    def _update_membership(self, subs_nz: Tuple[np.ndarray],
                           m: int) -> np.ndarray:
        """
        Return the Khatri-Rao product (sparse version) used in the update of the membership
        matrices.

        Parameters
        ----------
        subs_nz : tuple
                  Indices of elements of data that are non-zero.
        m : int
            Mode in which the Khatri-Rao product of the membership matrix is multiplied with the
             tensor: if 1 it
            works with the matrix u; if 2 it works with v.

        Returns
        -------
        uttkrp_DK : ndarray
                    Matrix which is the result of the matrix product of the unfolding of the
                    tensor and the
                    Khatri-Rao product of the membership matrix.
        """

        if not self.assortative:
            uttkrp_DK = sp_uttkrp(self.data_M_nz, subs_nz, m, self.u, self.v,
                                  self.w)
        else:
            uttkrp_DK = sp_uttkrp_assortative(self.data_M_nz, subs_nz, m,
                                              self.u, self.v, self.w)

        return uttkrp_DK

    def _PSLikelihood(self, data: Union[skt.dtensor, skt.sptensor],
                      data_T: skt.sptensor,
                      mask: Optional[np.ndarray] = None) -> float:
        """
        Compute the pseudo log-likelihood of the data.

        Parameters
        ----------
        data : sptensor/dtensor
               Graph adjacency tensor.
        data_T : sptensor/dtensor
                 Graph adjacency tensor (transpose).
        mask : ndarray
               Mask for selecting the held out set in the adjacency tensor in case of
               cross-validation.

        Returns
        -------
        l : float
            Pseudo log-likelihood value.
        """

        self.lambda0_ija = _lambda0_full(self.u, self.v, self.w)

        if mask is not None:
            sub_mask_nz = mask.nonzero()
            if isinstance(data, skt.dtensor):
                l = -self.lambda0_ija[sub_mask_nz].sum(
                ) - self.eta * data_T[sub_mask_nz].sum()
            elif isinstance(data, skt.sptensor):
                l = -self.lambda0_ija[sub_mask_nz].sum(
                ) - self.eta * data_T.toarray()[sub_mask_nz].sum()
        else:
            if isinstance(data, skt.dtensor):
                l = -self.lambda0_ija.sum() - self.eta * data_T.sum()
            elif isinstance(data, skt.sptensor):
                l = -self.lambda0_ija.sum() - self.eta * data_T.vals.sum()
        logM = np.log(self.M_nz)
        if isinstance(data, skt.dtensor):
            Alog = data[data.nonzero()] * logM
        elif isinstance(data, skt.sptensor):
            Alog = data.vals * logM

        l += Alog.sum()

        if np.isnan(l):
            message = "PSLikelihood is NaN!!!!"
            error_type = ValueError
            log_and_raise_error(error_type, message)

        return l

    def get_max_label(self):
        return "maxPSL"
