#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

from typing import Any, Callable, Dict, List, Optional, Tuple

import torch
from botorch.acquisition.acquisition import AcquisitionFunction
from botorch.acquisition.objective import ConstrainedMCObjective, LinearMCObjective
from botorch.acquisition.utils import get_acquisition_function, get_infeasible_cost
from botorch.fit import fit_gpytorch_model
from botorch.models.gp_regression import FixedNoiseGP, SingleTaskGP
from botorch.models.gp_regression_fidelity import SingleTaskMultiFidelityGP
from botorch.models.gpytorch import GPyTorchModel
from botorch.models.model import Model
from botorch.models.model_list_gp_regression import ModelListGP
from botorch.models.multitask import FixedNoiseMultiTaskGP, MultiTaskGP
from botorch.optim.optimize import optimize_acqf
from botorch.utils import (
    get_objective_weights_transform,
    get_outcome_constraint_transforms,
)
from gpytorch.mlls.exact_marginal_log_likelihood import ExactMarginalLogLikelihood
from gpytorch.mlls.sum_marginal_log_likelihood import SumMarginalLogLikelihood
from torch import Tensor


MIN_OBSERVED_NOISE_LEVEL = 1e-7


def get_and_fit_model(
    Xs: List[Tensor],
    Ys: List[Tensor],
    Yvars: List[Tensor],
    task_features: List[int],
    fidelity_features: List[int],
    state_dict: Optional[Dict[str, Tensor]] = None,
    refit_model: bool = True,
    **kwargs: Any,
) -> GPyTorchModel:
    r"""Instantiates and fits a botorch ModelListGP using the given data.

    Args:
        Xs: List of X data, one tensor per outcome
        Ys: List of Y data, one tensor per outcome
        Yvars: List of observed variance of Ys.
        task_features: List of columns of X that are tasks.
        fidelity_features: List of columns of X that are fidelity parameters.
        state_dict: If provided, will set model parameters to this state
            dictionary. Otherwise, will fit the model.
        refit_model: Flag for refitting model.

    Returns:
        A fitted GPyTorchModel.
    """
    if len(fidelity_features) > 0 and len(task_features) > 0:
        raise NotImplementedError(
            "Currently do not support MF-GP models with task_features!"
        )
    if len(fidelity_features) > 1:
        raise NotImplementedError(
            "Fidelity MF-GP models currently support only a single fidelity parameter!"
        )
    if len(task_features) > 1:
        raise NotImplementedError(
            f"This model only supports 1 task feature (got {task_features})"
        )
    elif len(task_features) == 1:
        task_feature = task_features[0]
    else:
        task_feature = None
    model = None
    if task_feature is None:
        if len(Xs) == 1:
            # Use single output, single task GP
            model = _get_model(
                X=Xs[0],
                Y=Ys[0],
                Yvar=Yvars[0],
                task_feature=task_feature,
                fidelity_features=fidelity_features,
                **kwargs,
            )
        elif all(torch.equal(Xs[0], X) for X in Xs[1:]):
            # Use batched multioutput, single task GP
            Y = torch.cat(Ys, dim=-1)
            Yvar = torch.cat(Yvars, dim=-1)
            model = _get_model(
                X=Xs[0],
                Y=Y,
                Yvar=Yvar,
                task_feature=task_feature,
                fidelity_features=fidelity_features,
                **kwargs,
            )
    if model is None:
        # Use a ModelListGP
        models = [
            _get_model(X=X, Y=Y, Yvar=Yvar, task_feature=task_feature, **kwargs)
            for X, Y, Yvar in zip(Xs, Ys, Yvars)
        ]
        model = ModelListGP(*models)
    model.to(Xs[0])
    if state_dict is not None:
        model.load_state_dict(state_dict)
    if state_dict is None or refit_model:
        # TODO: Add bounds for optimization stability - requires revamp upstream
        bounds = {}
        if isinstance(model, ModelListGP):
            mll = SumMarginalLogLikelihood(model.likelihood, model)
        else:
            # pyre-ignore: [16]
            mll = ExactMarginalLogLikelihood(model.likelihood, model)
        mll = fit_gpytorch_model(mll, bounds=bounds)
    return model


def predict_from_model(model: Model, X: Tensor) -> Tuple[Tensor, Tensor]:
    r"""Predicts outcomes given a model and input tensor.

    Args:
        model: A botorch Model.
        X: A `n x d` tensor of input parameters.

    Returns:
        Tensor: The predicted posterior mean as an `n x o`-dim tensor.
        Tensor: The predicted posterior covariance as a `n x o x o`-dim tensor.
    """
    with torch.no_grad():
        posterior = model.posterior(X)
    mean = posterior.mean.cpu().detach()
    # TODO: Allow Posterior to (optionally) return the full covariance matrix
    variance = posterior.variance.cpu().detach().clamp_min(0)  # pyre-ignore
    cov = torch.diag_embed(variance)
    return mean, cov


def get_NEI(
    model: Model,
    objective_weights: Tensor,
    outcome_constraints: Optional[Tuple[Tensor, Tensor]] = None,
    X_observed: Optional[Tensor] = None,
    X_pending: Optional[Tensor] = None,
    **kwargs: Any,
) -> AcquisitionFunction:
    r"""Instantiates a qNoisyExpectedImprovement acquisition function.

    Args:
        objective_weights: The objective is to maximize a weighted sum of
            the columns of f(x). These are the weights.
        outcome_constraints: A tuple of (A, b). For k outcome constraints
            and m outputs at f(x), A is (k x m) and b is (k x 1) such that
            A f(x) <= b. (Not used by single task models)
        X_observed: A tensor containing points observed for all objective
            outcomes and outcomes that appear in the outcome constraints (if
            there are any).
        X_pending: A tensor containing points whose evaluation is pending (i.e.
            that have been submitted for evaluation) present for all objective
            outcomes and outcomes that appear in the outcome constraints (if
            there are any).
        mc_samples: The number of MC samples to use (default: 512).
        qmc: If True, use qMC instead of MC (default: True).
        prune_baseline: If True, prune the baseline points for NEI (default: True).

    Returns:
        qNoisyExpectedImprovement: The instantiated acquisition function.
    """
    if X_observed is None:
        raise ValueError("There are no feasible observed points.")
    # construct Objective module
    if outcome_constraints is None:
        objective = LinearMCObjective(weights=objective_weights)
    else:
        obj_tf = get_objective_weights_transform(objective_weights)
        con_tfs = get_outcome_constraint_transforms(outcome_constraints)
        X_observed = torch.as_tensor(X_observed)
        inf_cost = get_infeasible_cost(X=X_observed, model=model, objective=obj_tf)
        objective = ConstrainedMCObjective(
            objective=obj_tf, constraints=con_tfs or [], infeasible_cost=inf_cost
        )
    return get_acquisition_function(
        acquisition_function_name="qNEI",
        model=model,
        objective=objective,
        X_observed=X_observed,
        X_pending=X_pending,
        prune_baseline=kwargs.get("prune_baseline", True),
        mc_samples=kwargs.get("mc_samples", 512),
        qmc=kwargs.get("qmc", True),
        seed=torch.randint(1, 10000, (1,)).item(),
    )


def scipy_optimizer(
    acq_function: AcquisitionFunction,
    bounds: Tensor,
    n: int,
    inequality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    fixed_features: Optional[Dict[int, float]] = None,
    rounding_func: Optional[Callable[[Tensor], Tensor]] = None,
    **kwargs: Any,
) -> Tuple[Tensor, Tensor]:
    r"""Optimizer using scipy's minimize module on a numpy-adpator.

    Args:
        acq_function: A botorch AcquisitionFunction.
        bounds: A `2 x d`-dim tensor, where `bounds[0]` (`bounds[1]`) are the
            lower (upper) bounds of the feasible hyperrectangle.
        n: The number of candidates to generate.
        inequality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `\sum_i (X[indices[i]] * coefficients[i]) >= rhs`
        fixed_features: A map {feature_index: value} for features that should
            be fixed to a particular value during generation.
        rounding_func: A function that rounds an optimization result
            appropriately (i.e., according to `round-trip` transformations).

    Returns:
        2-element tuple containing

        - A `n x d`-dim tensor of generated candidates.
        - In the case of joint optimization, a scalar tensor containing
          the joint acquisition value of the `n` points. In the case of
          sequential optimization, a `n`-dim tensor of conditional acquisition
          values, where `i`-th element is the expected acquisition value
          conditional on having observed candidates `0,1,...,i-1`.
    """

    num_restarts: int = kwargs.get("num_restarts", 20)
    raw_samples: int = kwargs.get("num_raw_samples", 50 * num_restarts)

    if kwargs.get("joint_optimization", False):
        sequential = False
    else:
        sequential = True
        # use SLSQP by default for small problems since it yields faster wall times
        if "method" not in kwargs:
            kwargs["method"] = "SLSQP"
    X, expected_acquisition_value = optimize_acqf(
        acq_function=acq_function,
        bounds=bounds,
        q=n,
        num_restarts=num_restarts,
        raw_samples=raw_samples,
        options=kwargs,
        inequality_constraints=inequality_constraints,
        fixed_features=fixed_features,
        sequential=sequential,
    )
    return X, expected_acquisition_value


def _get_model(
    X: Tensor,
    Y: Tensor,
    Yvar: Tensor,
    task_feature: Optional[int] = None,
    fidelity_features: Optional[List[int]] = None,
    **kwargs: Any,
) -> GPyTorchModel:
    """Instantiate a model of type depending on the input data.

    Args:
        X: A `n x d` tensor of input features.
        Y: A `n x m` tensor of input observations.
        Yvar: A `n x m` tensor of input variances (NaN if unobserved).
        task_feature: The index of the column pertaining to the task feature
            (if present).
        fidelity_features: List of columns of X that are fidelity parameters.

    Returns:
        A GPyTorchModel (unfitted).
    """
    Yvar = Yvar.clamp_min_(MIN_OBSERVED_NOISE_LEVEL)
    is_nan = torch.isnan(Yvar)
    any_nan_Yvar = torch.any(is_nan)
    all_nan_Yvar = torch.all(is_nan)
    if any_nan_Yvar and not all_nan_Yvar:
        raise ValueError(
            "Mix of known and unknown variances indicates valuation function "
            "errors. Variances should all be specified, or none should be."
        )
    if fidelity_features is None:
        fidelity_features = []
    if len(fidelity_features) > 0:
        if task_feature:
            raise NotImplementedError(
                "multi-task multi-fidelity models not yet available"
            )
        # at this point we can assume that there is only a single fidelity parameter
        gp = SingleTaskMultiFidelityGP(
            train_X=X, train_Y=Y, data_fidelity=fidelity_features[0], **kwargs
        )
    elif task_feature is None and all_nan_Yvar:
        gp = SingleTaskGP(train_X=X, train_Y=Y, **kwargs)
    elif task_feature is None:
        gp = FixedNoiseGP(train_X=X, train_Y=Y, train_Yvar=Yvar, **kwargs)
    elif all_nan_Yvar:
        gp = MultiTaskGP(train_X=X, train_Y=Y, task_feature=task_feature, **kwargs)
    else:
        gp = FixedNoiseMultiTaskGP(
            train_X=X,
            train_Y=Y.view(-1),
            train_Yvar=Yvar.view(-1),
            task_feature=task_feature,
            **kwargs,
        )
    return gp
