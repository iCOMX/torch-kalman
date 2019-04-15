from typing import Optional, Sequence, Tuple, Union

import torch
from torch import Tensor

from torch_kalman.design import Design
from torch_kalman.state_belief import StateBelief
from torch_kalman.state_belief.families.censored_gaussian.utils import tobit_adjustment, tobit_probs, std_normal
from torch_kalman.state_belief.families.gaussian import Gaussian, GaussianOverTime
from torch_kalman.state_belief.utils import bmat_idx
from torch_kalman.utils import matrix_diag

Selector = Union[Sequence[int], slice]


class CensoredGaussian(Gaussian):

    @classmethod
    def get_input_dim(cls, input: Union[Tensor, Tuple[Tensor, Tensor, Tensor]]) -> Tuple[int, int, int]:
        if isinstance(input, Tensor):
            return input.shape
        elif isinstance(input, Sequence):
            if input[1] is not None:
                assert input[0].shape == input[1].shape
            if input[2] is not None:
                assert input[0].shape == input[2].shape
            return input[0].shape
        else:
            raise RuntimeError("Expected `input` to be either tuple of tensors or tensor.")

    def update_from_input(self, input: Union[Tensor, Tuple[Tensor, Tensor, Tensor]], time: int):
        if isinstance(input, Tensor):
            obs = input[:, time, :]
            upper = None
            lower = None
        elif isinstance(input, Sequence):
            obs, lower, upper = (x[:, time, :] if x is not None else None for x in input)
        else:
            raise RuntimeError("Expected `input` to be either tuple of tensors or tensor.")
        return self.update(obs=obs, lower=lower, upper=upper)

    def update(self,
               obs: Tensor,
               lower: Optional[Tensor] = None,
               upper: Optional[Tensor] = None) -> 'StateBelief':
        return super().update(obs, lower=lower, upper=upper)

    def _update_group(self,
                      obs: Tensor,
                      group_idx: Union[slice, Sequence[int]],
                      which_valid: Union[slice, Sequence[int]],
                      lower: Optional[Tensor] = None,
                      upper: Optional[Tensor] = None) -> Tuple[Tensor, Tensor]:
        # indices:
        idx_2d = bmat_idx(group_idx, which_valid)
        idx_3d = bmat_idx(group_idx, which_valid, which_valid)

        # observed values, censoring limits
        vals = obs[idx_2d]

        # subset belief / design-mats:
        means = self.means[group_idx]
        covs = self.covs[group_idx]
        R = self.R[idx_3d]
        H = self.H[idx_2d]
        measured_means = H.matmul(means.unsqueeze(2)).squeeze(2)

        # calculate censoring fx:
        prob_lo, prob_up = tobit_probs(measured_means, R, lower=lower, upper=upper)
        prob_obs = matrix_diag(1 - prob_up - prob_lo)

        mm_adj, R_adj = tobit_adjustment(mean=measured_means,
                                         cov=R,
                                         lower=lower,
                                         upper=upper)

        # kalman gain:
        K = self.kalman_gain(covariance=covs, H=H, R_adjusted=R_adj, prob_obs=prob_obs)

        # update
        means_new = self.mean_update(mean=means, K=K, residuals=vals - mm_adj)

        covs_new = self.covariance_update(covariance=covs, K=K, H=H, prob_obs=prob_obs)
        return means_new, covs_new

    def _update_last_measured(self, obs: Tensor) -> Tensor:
        if obs.ndimension() == 3:
            obs = obs[..., 0]
        any_measured_group_idx = (torch.sum(~torch.isnan(obs), 1) > 0).nonzero().squeeze(-1)
        last_measured = self.last_measured.clone()
        last_measured[any_measured_group_idx] = 0
        return last_measured

    @staticmethod
    def mean_update(mean: Tensor, K: Tensor, residuals: Tensor) -> Tensor:
        return mean + K.matmul(residuals.unsqueeze(2)).squeeze(2)

    @staticmethod
    def covariance_update(covariance: Tensor, H: Tensor, K: Tensor, prob_obs: Tensor) -> Tensor:
        num_groups, num_dim, *_ = covariance.shape
        I = torch.eye(num_dim, num_dim).expand(num_groups, -1, -1)
        k = (I - K.matmul(prob_obs).matmul(H))
        return k.matmul(covariance)

    # noinspection PyMethodOverriding
    @staticmethod
    def kalman_gain(covariance: Tensor,
                    H: Tensor,
                    R_adjusted: Tensor,
                    prob_obs: Tensor) -> Tensor:
        Ht = H.permute(0, 2, 1)
        state_uncertainty = covariance.matmul(Ht).matmul(prob_obs)
        system_uncertainty = prob_obs.matmul(H).matmul(covariance).matmul(Ht).matmul(prob_obs) + R_adjusted
        return state_uncertainty.matmul(torch.inverse(system_uncertainty))

    @classmethod
    def concatenate_over_time(cls,
                              state_beliefs: Sequence['CensoredGaussian'],
                              design: Design) -> 'CensoredGaussianOverTime':
        return CensoredGaussianOverTime(state_beliefs=state_beliefs, design=design)

    def sample_transition(self,
                          lower: Optional[Tensor] = None,
                          upper: Optional[Tensor] = None,
                          eps: Optional[Tensor] = None) -> Tensor:
        if lower is None and upper is None:
            return super().sample_transition(eps=eps)
        raise NotImplementedError


class CensoredGaussianOverTime(GaussianOverTime):
    def __init__(self,
                 state_beliefs: Sequence['CensoredGaussian'],
                 design: Design):
        super().__init__(state_beliefs=state_beliefs, design=design)

    def log_prob(self,
                 obs: Tensor,
                 lower: Optional[Tensor] = None,
                 upper: Optional[Tensor] = None,
                 method: str = 'independent'
                 ) -> Tensor:
        return super().log_prob(obs=obs, lower=lower, upper=upper, method=method)

    @staticmethod
    def _subset_obs_and_bounds(obs: Tensor,
                               lower: Optional[Tensor],
                               upper: Optional[Tensor],
                               idx_3d: Tuple) -> Tuple[Tensor, Tensor, Tensor]:

        obs = obs[idx_3d]

        if lower is None:
            lower = torch.empty_like(obs)
            lower[:] = float('-inf')
        else:
            assert not torch.isnan(lower).any(), "nans in 'lower'"
            lower = lower[idx_3d]
            assert (obs >= lower).all(), "Not all obs are >= lower censoring limit"

        if upper is None:
            upper = torch.empty_like(obs)
            upper[:] = float('inf')
        else:
            assert not torch.isnan(upper).any(), "nans in 'upper'"
            upper = upper[idx_3d]
            assert (obs <= upper).all(), "Not all obs are <= upper censoring limit"

        return obs, lower, upper

    def _log_prob_with_subsetting(self,
                                  obs: Tensor,
                                  group_idx: Selector,
                                  time_idx: Selector,
                                  measure_idx: Selector,
                                  method: str = 'independent',
                                  lower: Optional[Tensor] = None,
                                  upper: Optional[Tensor] = None) -> Tensor:
        self._check_lp_sub_input(group_idx, time_idx)

        idx_no_measure = bmat_idx(group_idx, time_idx)
        idx_3d = bmat_idx(group_idx, time_idx, measure_idx)
        idx_4d = bmat_idx(group_idx, time_idx, measure_idx, measure_idx)

        # subset obs, lower, upper:
        group_obs, group_lower, group_upper = self._subset_obs_and_bounds(obs, lower, upper, idx_3d)

        if method.lower() == 'update':
            group_means = self.means[idx_no_measure]
            group_covs = self.covs[idx_no_measure]
            group_H = self.H[idx_3d]
            group_R = self.R[idx_4d]
            group_measured_means = group_H.matmul(group_means.unsqueeze(3)).squeeze(3)

            # calculate adjusted measure mean and cov:
            mm_adj, R_adj = tobit_adjustment(mean=group_measured_means,
                                             cov=group_R,
                                             lower=group_lower,
                                             upper=group_upper)

            # calculate prob-obs:
            prob_lo, prob_up = tobit_probs(group_measured_means, group_R, lower=group_lower, upper=group_upper)
            prob_obs = matrix_diag(1 - prob_up - prob_lo)

            # system uncertainty:
            Ht = group_H.permute(0, 1, 3, 2)
            system_uncertainty = prob_obs.matmul(group_H).matmul(group_covs).matmul(Ht).matmul(prob_obs) + R_adj

            # log prob:
            dist = torch.distributions.MultivariateNormal(mm_adj, system_uncertainty)
            return dist.log_prob(group_obs)
        elif method.lower() == 'independent':
            #
            pred_mean = self.predictions[idx_3d]
            pred_cov = self.prediction_uncertainty[idx_4d]

            #
            cens_up = (group_obs == group_upper)
            cens_lo = (group_obs == group_lower)

            #
            lik_uncens = torch.zeros_like(group_obs)
            lik_cens_up = torch.zeros_like(group_obs)
            lik_cens_lo = torch.zeros_like(group_obs)
            for m in range(pred_mean.shape[-1]):
                std = pred_cov[..., m, m].sqrt()
                z = (pred_mean[..., m] - group_obs[..., m]) / std

                # pdf is well behaved at tails:
                lik_uncens[..., m] = torch.exp(std_normal.log_prob(z) - std.log())

                # but cdf is not, truncate:
                z = torch.clamp(z, -5., 5.)
                lik_cens_up[..., m] = std_normal.cdf(z)
                lik_cens_lo[..., m] = 1. - std_normal.cdf(z)

            lik_cens = torch.zeros_like(group_obs)
            lik_cens[cens_up] = lik_cens_up[cens_up]
            lik_cens[cens_lo] = lik_cens_lo[cens_lo]

            log_prob = (lik_cens + lik_uncens).log()

            # take the product of the dimension probs (i.e., assume independence)
            return torch.sum(log_prob, -1)
        else:
            raise RuntimeError("Expected method to be one of: {}.".format({'update', 'independent'}))

    def sample_measurements(self,
                            lower: Optional[Tensor] = None,
                            upper: Optional[Tensor] = None,
                            eps: Optional[Tensor] = None):
        if lower is None and upper is None:
            return super().sample_measurements(eps=eps)
        raise NotImplementedError