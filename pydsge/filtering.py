#!/bin/python2
# -*- coding: utf-8 -*-

import numpy as np
from .stuff import *
import pydsge
# from econsieve import UnscentedKalmanFilter as UKF
# from econsieve import ScaledSigmaPoints as SSP
from econsieve import EnKF
from econsieve.stats import logpdf

def create_obs_cov(self, scale_obs = 0.1):

    if not hasattr(self, 'Z'):
        warnings.warn('No time series of observables provided')
    else:
        sig_obs 	= np.var(self.Z, axis = 0)*scale_obs

        self.obs_cov   = np.diagflat(sig_obs)


def create_filter(self, P = None, R = None, N = None):

    np.random.seed(0)

    if not hasattr(self, 'Z'):
        warnings.warn('No time series of observables provided')

    if N is None:
        N   = 5*len(self.vv)

    enkf    = EnKF(N, model_obj = self)

    if P is not None:
        enkf.P 		= P
    else:
        enkf.P 		*= 1e1

    if R is not None:
        enkf.R 		= R
    elif hasattr(self, 'obs_cov'):
        enkf.R 		= self.obs_cov

    CO          = self.SIG @ self.QQ(self.par)

    enkf.Q 		= CO @ CO.T

    self.enkf    = enkf


def get_ll(self, verbose = False, use_bruite = 0):

    if verbose:
        st  = time.time()

    ## set approximation to get ll
    self.enkf.fx    = lambda x: self.t_func(x, use_bruite = use_bruite)

    ll     = self.enkf.batch_filter(self.Z, calc_ll = True, verbose=verbose)[2]

    self.ll     = ll

    if verbose:
        print('[get_ll:]'.ljust(15, ' ')+'Filtering done in '+str(np.round(time.time()-st,3))+'seconds.')

    return self.ll


def run_filter(self, use_rts=True, verbose=False, use_bruite = 1):

    ## set approximation level
    self.enkf.fx    = lambda x: self.t_func(x, use_bruite = use_bruite)

    if verbose:
        st  = time.time()

    X1, cov, _      = self.enkf.batch_filter(self.Z, store=use_rts, verbose=verbose)

    if use_rts:
        X1, cov     = self.enkf.rts_smoother(X1, cov)

    if verbose:
        print('[run_filter:]'.ljust(15, ' ')+'Filtering done in '+str(np.round(time.time()-st,3))+'seconds.')

    self.filtered_X     = X1
    self.filtered_cov   = cov
    
    return X1, cov


def extract(self, pmean = None, cov = None, method = None, converged_only = False, return_flag = False, show_warnings = True, verbose = True):

    if pmean is None:
        pmean   = self.filtered_X.copy()

    if cov is None:
        cov     = self.filtered_cov

    means, cov, res, flag   = self.enkf.ipas(pmean, cov, method, converged_only, show_warnings = show_warnings, return_flag = True, verbose = verbose)

    self.res            = res

    if return_flag:
        return means, cov, res, flag

    return means, cov, res
