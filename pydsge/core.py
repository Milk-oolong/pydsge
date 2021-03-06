#!/bin/python
# -*- coding: utf-8 -*-

"""contains functions related to (re)compiling the model with different parameters
"""

from grgrlib import fast0, eig, re_bk
import numpy as np
import numpy.linalg as nl
import scipy.linalg as sl
import time
from .engine import preprocess
from .stats import post_mean

try:
    from numpy.core._exceptions import UFuncTypeError as ParafuncError
except ModuleNotFoundError:
    ParafuncError = Exception


def get_sys(self, par=None, reduce_sys=None, l_max=None, k_max=None, linear=False, tol=1e-8, ignore_tests=False, verbose=False):
    """Creates the transition function given a set of parameters. 

    If no parameters are given this will default to the calibration in the `yaml` file.

    Parameters
    ----------
    par : array or list, optional
        The parameters to parse into the transition function. (defaults to calibration in `yaml`)
    reduce_sys : bool, optional
        If true, the state space is reduced. This speeds up computation.
    l_max : int, optional
        The expected number of periods *until* the constraint binds (defaults to 3).
    k_max : int, optional
        The expected number of periods for which the constraint binds (defaults to 17).
    """

    st = time.time()

    reduce_sys = reduce_sys if reduce_sys is not None else self.fdict.get(
        'reduce_sys')
    ignore_tests = ignore_tests if ignore_tests is not None else self.fdict.get(
        'ignore_tests')

    if l_max is not None:
        if l_max < 2:
            print('[get_sys:]'.ljust(15, ' ') +
                  ' `l_max` must be at least 2 (is %s). Correcting...' % l_max)
            l_max = 2
        # effective l_max is one lower because algorithm exists on l_max 
        l_max += 1

    elif hasattr(self, 'lks'):
        l_max = self.lks[0]
    else:
        l_max = 1 if linear else 3

    if k_max is not None:
        pass
    elif hasattr(self, 'lks'):
        k_max = self.lks[1]
    else:
        k_max = 0 if linear else 17 

    self.lks = [l_max, k_max]

    self.fdict['reduce_sys'] = reduce_sys
    self.fdict['ignore_tests'] = ignore_tests

    par = self.p0() if par is None else list(par)
    try:
        ppar = self.pcompile(par)  # parsed par
    except AttributeError:
        ppar = self.compile(par)  # parsed par

    self.par = par
    self.ppar = ppar

    if not self.const_var:
        raise NotImplementedError('Package is only meant to work with OBCs')

    vv_v = np.array([v.name for v in self.variables])
    vv_x = np.array(self.variables)

    dim_v = len(vv_v)

    # obtain matrices
    AA = self.AA(ppar)              # forward
    BB = self.BB(ppar)              # contemp
    CC = self.CC(ppar)              # backward
    bb = self.bb(ppar).flatten().astype(float)  # constraint

    # define transition shocks -> state
    D = self.PSI(ppar)

    # mask those vars that are either forward looking or part of the constraint
    in_x = ~fast0(AA, 0) | ~fast0(bb[:dim_v])

    # reduce x vector
    vv_x2 = vv_x[in_x]
    A1 = AA[:, in_x]
    b1 = np.hstack((bb[:dim_v][in_x], bb[dim_v:]))

    dim_x = len(vv_x2)

    # define actual matrices
    N = np.block([[np.zeros(A1.shape), CC], [
                 np.eye(dim_x), np.zeros((dim_x, dim_v))]])

    P = np.block([[-A1, -BB], [np.zeros((dim_x, dim_x)), np.eye(dim_v)[in_x]]])

    c_arg = list(vv_x2).index(self.const_var)

    # c contains information on how the constraint var affects the system
    c1 = N[:, c_arg]
    c_P = P[:, c_arg]

    # get rid of constrained var
    b2 = np.delete(b1, c_arg)
    N1 = np.delete(N, c_arg, 1)
    P1 = np.delete(P, c_arg, 1)
    vv_x3 = np.delete(vv_x2, c_arg)
    dim_x = len(vv_x3)

    M1 = N1 + np.outer(c1, b2)

    # solve using Klein's method
    OME = re_bk(M1, P1, d_endo=dim_x)
    J = np.hstack((np.eye(dim_x), -OME))

    # desingularization of P
    U, s, V = nl.svd(P1)

    s0 = s < tol

    P2 = U.T @ P1
    N2 = U.T @ N1
    c2 = U.T @ c1

    # actual desingularization by iterating equations in M forward
    P2[s0] = N2[s0]

    # I could possible create auxiallary variables to make this work. Or I get the stuff directly from the boehlgo
    if not fast0(c2[s0], 2) or not fast0(U.T[s0] @ c_P, 2):
        raise NotImplementedError(
            'The system depends directly or indirectly on whether the constraint holds in the future or not.\n')

    if verbose > 1:
        print('[get_sys:]'.ljust(15, ' ') +
              ' determinant of `P` is %1.2e.' % nl.det(P2))

    if 'x_bar' in [p.name for p in self.parameters]:
        x_bar = par[[p.name for p in self.parameters].index('x_bar')]
    elif 'x_bar' in self.parafunc[0]:
        pf = self.parafunc
        x_bar = pf[1](par)[pf[0].index('x_bar')]
    else:
        print("Parameter `x_bar` (maximum value of the constraint) not specified. Assuming x_bar = -1 for now.")
        x_bar = -1

    try:
        cx = nl.inv(P2) @ c2*x_bar
    except ParafuncError:
        raise SyntaxError(
            "At least one parameter is a function of other parameters, and should be declared in `parafunc`.")

    # create the stuff that the algorithm needs
    N = nl.inv(P2) @ N2
    A = nl.inv(P2) @ (N2 + np.outer(c2, b2))

    out_msk = fast0(N, 0) & fast0(A, 0) & fast0(b2) & fast0(cx)
    out_msk[-len(vv_v):] = out_msk[-len(vv_v):] & fast0(self.ZZ(ppar), 0)
    # store those that are/could be reduced
    self.out_msk = out_msk[-len(vv_v):].copy()

    if not reduce_sys:
        out_msk[-len(vv_v):] = False

    s_out_msk = out_msk[-len(vv_v):]

    if hasattr(self, 'P'):
        if self.P.shape[0] < sum(~s_out_msk):
            P_new = np.zeros((len(self.out_msk), len(self.out_msk)))
            if P_new[~self.out_msk][:, ~self.out_msk].shape != self.P.shape:
                print('[get_sys:]'.ljust(
                    15, ' ')+' Shape missmatch of P-matrix, number of states seems to differ!')
            P_new[~self.out_msk][:, ~self.out_msk] = self.P
            self.P = P_new
        elif self.P.shape[0] > sum(~s_out_msk):
            self.P = self.P[~s_out_msk][:, ~s_out_msk]

    # add everything to the DSGE object
    self.vv = vv_v[~s_out_msk]
    self.vx = np.array([v.name for v in vv_x3])
    self.dim_x = dim_x
    self.dim_v = len(self.vv)

    self.hx = self.ZZ(ppar)[:, ~s_out_msk], self.DD(ppar).squeeze()
    self.obs_arg = np.where(self.hx[0])[1]

    N2 = N[~out_msk][:, ~out_msk]
    A2 = A[~out_msk][:, ~out_msk]
    J2 = J[:, ~out_msk]

    self.SIG = (BB.T @ D)[~s_out_msk]

    self.sys = N2, A2, J2, cx[~out_msk], b2[~out_msk], x_bar

    if verbose:
        print('[get_sys:]'.ljust(15, ' ')+' Creation of system matrices finished in %ss.'
              % np.round(time.time() - st, 3))

    preprocess(self, self.lks[0], self.lks[1], verbose)

    if not ignore_tests:
        test_obj = self.precalc_mat[0][1, 0, 1]
        test_con = eig(test_obj[-test_obj.shape[1]:]) > 1
        if test_con.any():
            raise ValueError(
                'Explosive dynamics detected: %s EV(s) > 1' % sum(test_con))

    return


def posterior_sampler(self, nsamples, seed=0, verbose=True):
    """Draw parameters from the posterior.

    Parameters
    ----------
    nsamples : int
        Size of the sample

    Returns
    -------
    array
        Numpy array of parameters
    """

    import random

    random.seed(seed)
    sample = self.get_chain()[-self.get_tune:]
    sample = sample.reshape(-1, sample.shape[-1])
    sample = random.choices(sample, k=nsamples)

    return sample


def sample_box(self, dim0, dim1=None, bounds=None, lp_rule=None, verbose=False):
    """Sample from a hypercube
    """

    # TODO: include in get_par

    import chaospy

    bnd = bounds or np.array(self.fdict['prior_bounds'])
    dim1 = dim1 or self.ndim
    rule = lp_rule or 'S'

    res = chaospy.Uniform(0, 1).sample(size=(dim0, dim1), rule=rule)
    res = (bnd[1] - bnd[0])*res + bnd[0]

    return res


def prior_sampler(self, nsamples, seed=0, test_lprob=False, lks=None, verbose=True, debug=False, **args):
    """Draw parameters from prior. 

    Parameters
    ----------
    nsamples : int
        Size of the prior sample
    seed : int, optional
        Set the random seed (0 by default)
    test_lprob : bool, optional
        Whether to ensure that drawn parameters have a finite likelihood (False by default)
    verbose : bool, optional
    debug : bool, optional

    Returns
    -------
    array
        Numpy array of parameters
    """

    import tqdm
    from grgrlib import map2arr, serializer
    from .stats import get_prior

    store_reduce_sys = np.copy(self.fdict['reduce_sys'])

    l_max, k_max = lks or (None, None)

    # if not store_reduce_sys:
        # self.get_sys(reduce_sys=True, verbose=verbose > 1, **args)

    if test_lprob and not hasattr(self, 'ndim'):
        self.prep_estim(load_R=True, verbose=verbose > 2)

    frozen_prior = get_prior(self.prior, verbose=verbose)[0]
    self.debug |= debug

    if hasattr(self, 'pool'):
        from .estimation import create_pool
        create_pool(self)

    set_par = serializer(self.set_par)
    get_par = serializer(self.get_par)
    lprob = serializer(self.lprob) if test_lprob else None

    def runner(locseed):

        np.random.seed(seed+locseed)
        done = False
        no = 0

        while not done:

            no += 1

            with np.warnings.catch_warnings(record=False):
                try:
                    np.warnings.filterwarnings('error')
                    rst = np.random.randint(2**31)  # win explodes with 2**32
                    pdraw = [pl.rvs(random_state=rst+sn)
                             for sn, pl in enumerate(frozen_prior)]

                    if test_lprob:
                        draw_prob = lprob(pdraw, linear=None,
                                          verbose=verbose > 1)
                        done = not np.isinf(draw_prob)
                    else:
                        set_par(pdraw)
                        done = True

                except Exception as e:
                    if verbose > 1:
                        print(str(e)+'(%s) ' % no)

        return pdraw, no

    if verbose > 1:
        print('[prior_sample:]'.ljust(15, ' ') + ' Sampling from the pior...')

    wrapper = tqdm.tqdm if verbose < 2 else (lambda x, **kwarg: x)
    pmap_sim = wrapper(self.mapper(runner, range(nsamples)), total=nsamples)

    draws, nos = map2arr(pmap_sim)

    # if not store_reduce_sys:
        # self.get_sys(reduce_sys=False, verbose=verbose > 1, **args)

    if verbose:
        smess = ''
        if test_lprob:
            smess = 'of zero likelihood, '
        print('[prior_sample:]'.ljust(
            15, ' ') + ' Sampling done. %2.2f%% of the prior is either %sindetermined or explosive.' % (100*(sum(nos)-nsamples)/sum(nos), smess))

    return draws


def get_par(self, dummy=None, npar=None, asdict=False, full=True, nsamples=1, verbose=False, roundto=5, debug=False, **args):
    """Get parameters. Tries to figure out what you want. 

    Parameters
    ----------
    dummy : str, optional
        Can be `None`, a parameter name, a parameter set out of {'calib', 'init', 'prior_mean', 'best', 'mode', 'mcmc_mode', 'post_mean', 'posterior_mean'} or one of {'prior', 'post', 'posterior'}. 

        If `None`, returns the current parameters (default). If there are no current parameters, this defaults to 'best'.
        'calib' will return the calibration in the main body of the *.yaml (`parameters`). 
        'init' are the initial values (first column) in the `prior` section of the *.yaml.
        'mode' is the highest known mode from any sort of parameter estimation.
        'best' will default to 'mode' if it exists and otherwise fall back to 'init'.
        'posterior_mean' and 'post_mean' are the same thing.
        'posterior_mode', 'post_mode' and 'mcmc_mode' are the same thing.
        'prior' or 'post'/'posterior' will draw random samples. Obviously, 'posterior', 'mode' etc are only available if a posterior/chain exists.

        NOTE: calling get_par with a set of parameters is the only way to recover the calibrated parameters that are not included in the prior (if you have changed them). All other options will work incrementially on (potential) previous edits of these parameters.

    asdict : bool, optional
        Returns a dict of the values if `True` and an array otherwise (default is `False`).
    full : bool, optional
        Whether to return all parameters or the estimated ones only. (default: True)
    nsamples : int, optional
        Size of the sample. Defaults to 1
    verbose : bool, optional
        Print additional output infmormation (default is `False`)
    roundto : int, optional
        Rounding of additional output if verbose, defaults to 5
    args : various, optional
        Auxilliary arguments passed to `get_sys` calls

    Returns
    -------
    array or dict
        Numpy array of parameters or dict of parameters
    """

    if not hasattr(self, 'par'):
        get_sys(self, verbose=verbose, **args)

    pfnames, pffunc = self.parafunc
    pars_str = [str(p) for p in self.parameters]

    pars = np.array(self.par) if hasattr(
        self, 'par') else np.array(self.par_fix)
    if npar is not None:
        if len(npar) != len(self.par_fix):
            pars[self.prior_arg] = npar
        else:
            pars = npar

    if dummy is None:
        try:
            par_cand = np.array(pars)[self.prior_arg]
        except:
            par_cand = get_par(self, 'best', asdict=False,
                               full=False, verbose=verbose, **args)
    elif not isinstance(dummy, str) and len(dummy) == len(self.par_fix):
        par_cand = dummy[self.prior_arg]
    elif not isinstance(dummy, str) and len(dummy) == len(self.prior_arg):
        par_cand = dummy
    elif dummy in pars_str:
        p = pars[pars_str.index(dummy)]
        if verbose:
            print('[get_par:]'.ljust(15, ' ') + "%s = %s" % (dummy, p))
        return p
    elif dummy in pfnames:
        p = pffunc(pars)[pfnames.index(dummy)]
        if verbose:
            print('[get_par:]'.ljust(15, ' ') + "%s = %s" % (dummy, p))
        return p
    elif dummy == 'cov_mat':
        get_sys(self, pars)
        p = self.QQ(self.ppar)
        if verbose:
            print('[get_par:]'.ljust(15, ' ') + "%s = %s" % (dummy, p))
        return p
    elif dummy == 'best':
        try:
            par_cand = get_par(self, 'mode', asdict=False,
                               full=False, verbose=verbose, **args)
        except:
            par_cand = get_par(self, 'init', asdict=False,
                               full=False, verbose=verbose, **args)
    else:
        # ensure that ALL parameters are reset, not only those included in the prior
        old_par = self.par
        pars = self.par_fix
        self.par = self.par_fix

        if dummy == 'prior':
            par_cand = prior_sampler(
                self, nsamples=nsamples, verbose=verbose, debug=debug, **args)
        elif dummy in ('post', 'posterior'):
            par_cand = posterior_sampler(
                self, nsamples=nsamples, verbose=verbose, **args)
        elif dummy == 'posterior_mean' or dummy == 'post_mean':
            par_cand = post_mean(self)
        elif dummy == 'mode':
            par_cand = self.fdict['mode_x']
        elif dummy in ('mcmc_mode', 'mode_mcmc', 'posterior_mode', 'post_mode'):
            par_cand = self.fdict['mcmc_mode_x']
        elif dummy == 'calib':
            par_cand = self.par_fix[self.prior_arg].copy()
        elif dummy == 'prior_mean':
            par_cand = []
            for pp in self.prior.keys():
                if self.prior[pp][3] == 'uniform':
                    par_cand.append(.5*self.prior[pp][-2] + .5*self.prior[pp][-1])
                else:
                    par_cand.append(self.prior[pp][-2])
        elif dummy == 'adj_prior_mean':
            # adjust for prior[pp][-2] not beeing the actual mean for inv_gamma_dynare
            par_cand = []
            for pp in self.prior.keys():
                if self.prior[pp][3] == 'inv_gamma_dynare':
                    par_cand.append(self.prior[pp][-2]*10)
                elif self.prior[pp][3] == 'uniform':
                    par_cand.append(.5*self.prior[pp][-2] + .5*self.prior[pp][-1])
                else:
                    par_cand.append(self.prior[pp][-2])
        elif dummy == 'init':
            par_cand = self.fdict['init_value']
            for i in range(self.ndim):
                if par_cand[i] is None:
                    par_cand[i] = self.par_fix[self.prior_arg][i]
        else:
            self.par = old_par
            raise KeyError(
                "Parameter or parametrization '%s' does not exist." % dummy)

    if full:
        if isinstance(dummy, str) and dummy in ('prior', 'post', 'posterior'):
            par = np.tile(pars, (nsamples, 1))
            for i in range(nsamples):
                par[i][self.prior_arg] = par_cand[i]
        else:
            par = np.array(pars)
            par[self.prior_arg] = par_cand

        if not asdict:
            return par

        pdict = dict(zip(pars_str, np.round(par, roundto)))
        pfdict = dict(zip(pfnames, np.round(pffunc(par), roundto)))

        return pdict, pfdict

    if asdict:
        return dict(zip(np.array(pars_str)[self.prior_arg], np.round(par_cand, roundto)))

    if nsamples > 1 and not dummy in ('prior', 'post', 'posterior'):
        par_cand = par_cand*(1 + 1e-3*np.random.randn(nsamples, len(par_cand)))

    return par_cand


def get_cov(self, npar=None, **args):
    """get the covariance matrix"""
    return get_par(self, dummy='cov_mat', npar=npar, **args)


def set_par(self, dummy=None, setpar=None, npar=None, verbose=False, roundto=5, **args):
    """Set the current parameter values.

    In essence, this is a wrapper around `get_par` which also compiles the transition function with the desired parameters.

    Parameters
    ----------
    dummy : str or array, optional
        If an array, sets all parameters. If a string and a parameter name,`setpar` must be provided to define the value of this parameter. Otherwise, `dummy` is forwarded to `get_par` and the returning value(s) are set as parameters.
    setpar : float, optional
        Parametervalue to be set. Of course, only if `dummy` is a parameter name.
    npar : array, optional
        Vector of parameters. If given, this vector will be altered and returnd without recompiling the model. THIS WILL ALTER THE PARAMTER WITHOUT MAKING A COPY!
    verbose : bool
        Whether to output more or less informative messages (defaults to False)
    roundto : int
        Define output precision if output is verbose. (default: 5)
    args : keyword args
        Keyword arguments forwarded to the `get_sys` call.
    """

    pfnames, pffunc = self.parafunc
    pars_str = [str(p) for p in self.parameters]
    par = np.array(self.par) if hasattr(
        self, 'par') else np.array(self.par_fix)

    if setpar is None:
        if dummy is None:
            par = get_par(self, dummy=dummy, asdict=False,
                          full=True, verbose=verbose, **args)
        elif len(dummy) == len(self.par_fix):
            par = dummy
        elif len(dummy) == len(self.prior_arg):
            par[self.prior_arg] = dummy
        else:
            par = get_par(self, dummy=dummy, asdict=False,
                          full=True, verbose=verbose, **args)
    elif dummy in pars_str:
        if npar is not None:
            npar = npar.copy()
            if len(npar) == len(self.prior_arg):
                npar[self.prior_names.index(dummy)] = setpar
            else:
                npar[pars_str.index(dummy)] = setpar
            return npar
        par[pars_str.index(dummy)] = setpar
    elif dummy in pfnames:
        raise SyntaxError(
            "Can not set parameter '%s' that is a function of other parameters." % dummy)
    else:
        raise SyntaxError(
            "Parameter '%s' is not defined for this model." % dummy)

    # do compile model only if not vector is given that should be altered
    get_sys(self, par=list(par), verbose=verbose, **args)

    if hasattr(self, 'filter'):

        self.filter.eps_cov = self.QQ(self.ppar)

        if self.filter.name == 'KalmanFilter':
            CO = self.SIG @ self.filter.eps_cov
            Q = CO @ CO.T
        elif self.filter.name == 'ParticleFilter':
            raise NotImplementedError
        else:
            Q = self.QQ(self.ppar) @ self.QQ(self.ppar)

        self.filter.Q = Q

    if verbose:
        pdict = dict(zip(pars_str, np.round(self.par, roundto)))
        pfdict = dict(zip(pfnames, np.round(pffunc(self.par), roundto)))

        print('[set_par:]'.ljust(15, ' ') +
              " Parameter(s):\n%s\n%s" % (pdict, pfdict))

    return get_par(self)
