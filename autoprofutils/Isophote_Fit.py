import numpy as np
from scipy.stats import iqr
from scipy.fftpack import fft, ifft
from scipy.optimize import minimize
from sklearn.preprocessing import PolynomialFeatures
from sklearn.pipeline import make_pipeline
from sklearn.linear_model import RANSACRegressor, HuberRegressor
from time import time
from astropy.visualization import SqrtStretch, LogStretch
from astropy.visualization.mpl_normalize import ImageNormalize
from photutils.isophote import EllipseSample, EllipseGeometry, Isophote, IsophoteList
from photutils.isophote import Ellipse as Photutils_Ellipse
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse
from copy import copy
import logging
import sys
import os
sys.path.append(os.environ['AUTOPROF'])
from autoprofutils.SharedFunctions import _iso_extract, _x_to_pa, _x_to_eps, _inv_x_to_eps, _inv_x_to_pa, Angle_TwoAngles, LSBImage, AddLogo, PA_shift_convention

def Photutils_Fit(IMG, results, options):
    """
    Function to run the photutils automated isophote analysis on an image.
    """    

    dat = IMG - results['background']
    geo = EllipseGeometry(x0 = results['center']['x'],
                          y0 = results['center']['y'],
                          sma = results['init R']/2,
                          eps = results['init ellip'],
                          pa = results['init pa'])
    ellipse = Photutils_Ellipse(dat, geometry = geo)

    isolist = ellipse.fit_image(fix_center = True, linear = False)
    res = {'fit R': isolist.sma[1:], 'fit ellip': isolist.eps[1:], 'fit ellip_err': isolist.ellip_err[1:],
           'fit pa': isolist.pa[1:], 'fit pa_err': isolist.pa_err[1:], 'auxfile fitlimit': 'fit limit semi-major axis: %.2f pix' % isolist.sma[-1]}
    
    if 'ap_doplot' in options and options['ap_doplot']:
        ranges = [[max(0,int(results['center']['y']-res['fit R'][-1]*1.2)), min(dat.shape[1],int(results['center']['y']+res['fit R'][-1]*1.2))],
                  [max(0,int(results['center']['x']-res['fit R'][-1]*1.2)), min(dat.shape[0],int(results['center']['x']+res['fit R'][-1]*1.2))]]
        LSBImage(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]], results['background noise'])
        for i in range(len(res['fit R'])):
            plt.gca().add_patch(Ellipse((int(res['fit R'][-1]*1.2),int(res['fit R'][-1]*1.2)), 2*res['fit R'][i], 2*res['fit R'][i]*(1. - res['fit ellip'][i]),
                                        res['fit pa'][i]*180/np.pi, fill = False, linewidth = 0.5, color = 'r'))
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sfit_ellipse_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = 300)
        plt.close()                

    return IMG, res


def _ellip_smooth(R, E, deg):
    model = make_pipeline(PolynomialFeatures(deg), HuberRegressor(epsilon=2.))
    model.fit(np.log10(R).reshape(-1,1), _inv_x_to_eps(E))
    return _x_to_eps(model.predict(np.log10(R).reshape(-1,1)))
    
def _pa_smooth(R, PA, deg):

    model_s = make_pipeline(PolynomialFeatures(deg), HuberRegressor())
    model_c = make_pipeline(PolynomialFeatures(deg), HuberRegressor())
    model_c.fit(np.log10(R).reshape(-1,1), np.cos(2*PA))
    model_s.fit(np.log10(R).reshape(-1,1), np.sin(2*PA))
    pred_pa_s = np.clip(model_s.predict(np.log10(R).reshape(-1,1)), a_min = -1, a_max = 1)
    pred_pa_c = np.clip(model_c.predict(np.log10(R).reshape(-1,1)), a_min = -1, a_max = 1)

    return ((np.arctan(pred_pa_s/pred_pa_c) + (np.pi*(pred_pa_c < 0))) % (2*np.pi))/2
    

def _FFT_Robust_loss(dat, R, E, PA, i, C, noise, mask = None, reg_scale = 1., name = ''):

    isovals = _iso_extract(dat,R[i],E[i],PA[i],C, mask = mask, interp_mask = False if mask is None else True)
    
    if mask is None:
        coefs = fft(np.clip(isovals, a_max = np.quantile(isovals,0.85), a_min = None))
    else:
        coefs = fft(np.clip(isovals, a_max = np.quantile(isovals,0.9), a_min = None))
    
    f2_loss = np.abs(coefs[2]) / (len(isovals)*(max(0,np.median(isovals)) + noise))

    reg_loss = 0
    if i < (len(R)-1):
        reg_loss += abs((E[i] - E[i+1])/(1 - E[i+1])) 
        reg_loss += abs(Angle_TwoAngles(2*PA[i], 2*PA[i+1])/(2*0.2))
    if i > 0:
        reg_loss += abs((E[i] - E[i-1])/(1 - E[i-1])) 
        reg_loss += abs(Angle_TwoAngles(2*PA[i], 2*PA[i-1])/(2*0.2))

    return f2_loss*(1 + reg_loss*reg_scale) 

def Isophote_Fit_FFT_Robust(IMG, results, options):
    """
    Fit isophotes by minimizing the amplitude of the second FFT coefficient, relative to the local median flux.
    Included is a regularization term which penalizes isophotes for having large differences between parameters
    of adjacent isophotes.
    """

    if 'ap_scale' in options:
        scale = options['ap_scale']
    else:
        scale = 0.2

    # subtract background from image during processing
    dat = IMG - results['background']
    mask = results['mask'] if 'mask' in results else None
    if not np.any(mask):
        mask = None
    
    # Determine sampling radii
    ######################################################################
    shrink = 0
    while shrink < 5:
        sample_radii = [3*results['psf fwhm']/2]
        while sample_radii[-1] < (max(IMG.shape)/2):
            isovals = _iso_extract(dat,sample_radii[-1],results['init ellip'],
                                   results['init pa'],results['center'], more = False, mask = mask)
            if np.median(isovals) < (options['ap_fit_limit'] if 'ap_fit_limit' in options else 2)*results['background noise']:
                break
            sample_radii.append(sample_radii[-1]*(1.+scale/(1.+shrink)))
        if len(sample_radii) < 15:
            shrink += 1
        else:
            break
    if shrink >= 5:
        raise Exception('Unable to initialize ellipse fit, check diagnostic plots. Possible missed center.')
    ellip = np.ones(len(sample_radii))*results['init ellip']
    pa = np.ones(len(sample_radii))*results['init pa']
    logging.debug('%s: sample radii: %s' % (options['ap_name'], str(sample_radii)))
    
    # Fit isophotes
    ######################################################################
    perturb_scale = np.array([0.03, 0.06])
    regularize_scale = options['ap_regularize_scale'] if 'ap_regularize_scale' in options else 1.
    N_perturb = 5

    count = 0

    count_nochange = 0
    use_center = copy(results['center'])
    I = np.array(range(len(sample_radii)))
    while count < 300 and count_nochange < (3*len(sample_radii)):
        # Periodically include logging message
        if count % 10 == 0:
            logging.debug('%s: count: %i' % (options['ap_name'],count))
        count += 1
        
        np.random.shuffle(I)
        for i in I:
            perturbations = []
            perturbations.append({'ellip': copy(ellip), 'pa': copy(pa)})
            perturbations[-1]['loss'] = _FFT_Robust_loss(dat, sample_radii, perturbations[-1]['ellip'], perturbations[-1]['pa'], i,
                                                         use_center, results['background noise'], mask = mask, reg_scale = regularize_scale if count > 4 else 0, name = options['ap_name'])
            for n in range(N_perturb):
                perturbations.append({'ellip': copy(ellip), 'pa': copy(pa)})
                if count % 3 in [0,1]:
                    perturbations[-1]['ellip'][i] = _x_to_eps(_inv_x_to_eps(perturbations[-1]['ellip'][i]) + np.random.normal(loc = 0, scale = perturb_scale[0]))
                if count % 3 in [1,2]:
                    perturbations[-1]['pa'][i] = (perturbations[-1]['pa'][i] + np.random.normal(loc = 0, scale = perturb_scale[1])) % np.pi
                perturbations[-1]['loss'] = _FFT_Robust_loss(dat, sample_radii, perturbations[-1]['ellip'], perturbations[-1]['pa'], i,
                                                             use_center, results['background noise'], mask = mask, reg_scale = regularize_scale if count > 4 else 0, name = options['ap_name'])
            
            best = np.argmin(list(p['loss'] for p in perturbations))
            if best > 0:
                ellip = copy(perturbations[best]['ellip'])
                pa = copy(perturbations[best]['pa'])
                count_nochange = 0
            else:
                count_nochange += 1
                
    logging.info('%s: Completed isohpote fit in %i itterations' % (options['ap_name'], count))
    # detect collapsed center
    ######################################################################
    for i in range(5):
        if (_inv_x_to_eps(ellip[i]) - _inv_x_to_eps(ellip[i+1])) > 0.5:
            ellip[:i+1] = ellip[i+1]
            pa[:i+1] = pa[i+1]

    # Smooth ellip and pa profile
    ######################################################################
    smooth_ellip = copy(ellip)
    smooth_pa = copy(pa)
    ellip[:3] = min(ellip[:3])
    smooth_ellip = _ellip_smooth(sample_radii, smooth_ellip, 5)
    smooth_pa = _pa_smooth(sample_radii, smooth_pa, 5)
    
    if 'ap_doplot' in options and options['ap_doplot']:
        ranges = [[max(0,int(use_center['x']-sample_radii[-1]*1.2)), min(dat.shape[1],int(use_center['x']+sample_radii[-1]*1.2))],
                  [max(0,int(use_center['y']-sample_radii[-1]*1.2)), min(dat.shape[0],int(use_center['y']+sample_radii[-1]*1.2))]]
        LSBImage(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]], results['background noise'])
        # plt.imshow(np.clip(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]],
        #                    a_min = 0,a_max = None), origin = 'lower', cmap = 'Greys', norm = ImageNormalize(stretch=LogStretch())) 
        for i in range(len(sample_radii)):
            plt.gca().add_patch(Ellipse((use_center['x'] - ranges[0][0],use_center['y'] - ranges[1][0]), 2*sample_radii[i], 2*sample_radii[i]*(1. - ellip[i]),
                                        pa[i]*180/np.pi, fill = False, linewidth = ((i+1)/len(sample_radii))**2, color = 'r'))
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sfit_ellipse_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi'in options else 300)
        plt.close()
        
        plt.scatter(sample_radii, ellip, color = 'r', label = 'ellip')
        plt.scatter(sample_radii, pa/np.pi, color = 'b', label = 'pa/$np.pi$')
        show_ellip = _ellip_smooth(sample_radii, ellip, deg = 5)
        show_pa = _pa_smooth(sample_radii, pa, deg = 5)
        plt.plot(sample_radii, show_ellip, color = 'orange', linewidth = 2, linestyle='--', label = 'smooth ellip')
        plt.plot(sample_radii, show_pa/np.pi, color = 'purple', linewidth = 2, linestyle='--', label = 'smooth pa/$np.pi$')
        #plt.xscale('log')
        plt.legend()
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sphaseprofile_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi'in options else 300)
        plt.close()

    # Compute errors
    ######################################################################
    ellip_err = np.zeros(len(ellip))
    ellip_err[:2] = np.sqrt(np.sum((ellip[:4] - smooth_ellip[:4])**2)/4)
    ellip_err[-1] = np.sqrt(np.sum((ellip[-4:] - smooth_ellip[-4:])**2)/4)
    pa_err = np.zeros(len(pa))
    pa_err[:2] = np.sqrt(np.sum((pa[:4] - smooth_pa[:4])**2)/4)
    pa_err[-1] = np.sqrt(np.sum((pa[-4:] - smooth_pa[-4:])**2)/4)
    for i in range(2,len(pa)-1):
        ellip_err[i] = np.sqrt(np.sum((ellip[i-2:i+2] - smooth_ellip[i-2:i+2])**2)/4)
        pa_err[i] = np.sqrt(np.sum((pa[i-2:i+2] - smooth_pa[i-2:i+2])**2)/4)

    res = {'fit ellip': ellip, 'fit pa': pa, 'fit R': sample_radii,
           'fit ellip_err': ellip_err, 'fit pa_err': pa_err,
           'auxfile fitlimit': 'fit limit semi-major axis: %.2f pix' % sample_radii[-1]}
    return IMG, res

def Isophote_Fit_Forced(IMG, results, options):
    """
    Take isophotal fit from a given profile.
    """
    with open(options['ap_forcing_profile'], 'r') as f:
        raw = f.readlines()
        for i,l in enumerate(raw):
            if l[0] != '#':
                readfrom = i
                break
        header = list(h.strip() for h in raw[readfrom].split(','))
        force = dict((h,[]) for h in header)
        for l in raw[readfrom+2:]:
            for d, h in zip(l.split(','), header):
                force[h].append(float(d.strip()))

    force['pa'] = PA_shift_convention(np.array(force['pa']), deg = True)
                
    if 'ap_doplot' in options and options['ap_doplot']:
        dat = IMG - results['background']
        ranges = [[max(0,int(results['center']['y'] - (np.array(force['R'])[-1]/options['ap_pixscale'])*1.2)), min(dat.shape[1],int(results['center']['y'] + (np.array(force['R'])[-1]/options['ap_pixscale'])*1.2))],
                  [max(0,int(results['center']['x'] - (np.array(force['R'])[-1]/options['ap_pixscale'])*1.2)), min(dat.shape[0],int(results['center']['x'] + (np.array(force['R'])[-1]/options['ap_pixscale'])*1.2))]]
        LSBImage(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]], results['background noise'])
        # plt.imshow(np.clip(dat[ranges[0][0]: ranges[0][1], ranges[1][0]: ranges[1][1]],
        #                    a_min = 0,a_max = None), origin = 'lower', cmap = 'Greys_r', norm = ImageNormalize(stretch=LogStretch())) 
        for i in range(0,len(np.array(force['R'])),2):
            plt.gca().add_patch(Ellipse((results['center']['x'] - ranges[0][0],results['center']['y'] - ranges[1][0]), 2*(np.array(force['R'])[i]/options['ap_pixscale']),
                                        2*(np.array(force['R'])[i]/options['ap_pixscale'])*(1. - force['ellip'][i]),
                                        force['pa'][i], fill = False, linewidth = 0.5, color = 'r'))
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sforcedfit_ellipse_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi'in options else 300)
        plt.close()                
    res = {'fit ellip': np.array(force['ellip']),
           'fit pa': np.array(force['pa'])*np.pi/180,
           'fit R': list(np.array(force['R'])/options['ap_pixscale'])}
    if 'ellip_e' in force and 'pa_e' in force:
        res['fit ellip_err'] = np.array(force['ellip_e'])
        res['fit pa_err'] = np.array(force['pa_e'])*np.pi/180
    return IMG, res


######################################################################
def _FFT_mean_loss(dat, R, E, PA, i, C, noise, mask = None, reg_scale = 1., name = ''):

    isovals = _iso_extract(dat,R[i],E[i],PA[i],C, mask = mask, interp_mask = False if mask is None else True)
    
    if not np.all(np.isfinite(isovals)):
        logging.warning('Failed to evaluate isophotal flux values, skipping this ellip/pa combination')
        return np.inf

    coefs = fft(isovals)
    
    f2_loss = np.abs(coefs[2]) / (len(isovals)*(max(0,np.mean(isovals)) + noise))

    reg_loss = 0
    if i < (len(R)-1):
        reg_loss += abs((E[i] - E[i+1])/(1 - E[i+1])) #abs((_inv_x_to_eps(E[i]) - _inv_x_to_eps(E[i+1]))/0.1)
        reg_loss += abs(Angle_TwoAngles(2*PA[i], 2*PA[i+1])/(2*0.3))
    if i > 0:
        reg_loss += abs((E[i] - E[i-1])/(1 - E[i-1])) #abs((_inv_x_to_eps(E[i]) - _inv_x_to_eps(E[i-1]))/0.1)
        reg_loss += abs(Angle_TwoAngles(2*PA[i], 2*PA[i-1])/(2*0.3))

    return f2_loss*(1 + reg_loss*reg_scale) #(np.abs(coefs[2])/(len(isovals)*(abs(np.median(isovals)))))*reg_loss*reg_scale

def Isophote_Fit_FFT_mean(IMG, results, options):
    """
    Fit isophotes by minimizing the amplitude of the second FFT coefficient, relative to the local median flux.
    Included is a regularization term which penalizes isophotes for having large differences between parameters
    of adjacent isophotes.
    """

    if 'ap_scale' in options:
        scale = options['ap_scale']
    else:
        scale = 0.2

    # subtract background from image during processing
    dat = IMG - results['background']
    mask = results['mask'] if 'mask' in results else None
    if not np.any(mask):
        mask = None
    
    # Determine sampling radii
    ######################################################################
    shrink = 0
    while shrink < 5:
        sample_radii = [3*results['psf fwhm']/2]
        while sample_radii[-1] < (max(IMG.shape)/2):
            isovals = _iso_extract(dat,sample_radii[-1],results['init ellip'],
                                   results['init pa'],results['center'], more = False, mask = mask)
            if np.mean(isovals) < (options['ap_fit_limit'] if 'ap_fit_limit' in options else 1)*results['background noise']:
                break
            sample_radii.append(sample_radii[-1]*(1.+scale/(1.+shrink)))
        if len(sample_radii) < 15:
            shrink += 1
        else:
            break
    if shrink >= 5:
        raise Exception('Unable to initialize ellipse fit, check diagnostic plots. Possible missed center.')
    ellip = np.ones(len(sample_radii))*results['init ellip']
    pa = np.ones(len(sample_radii))*results['init pa']
    logging.debug('%s: sample radii: %s' % (options['ap_name'], str(sample_radii)))
    
    # Fit isophotes
    ######################################################################
    perturb_scale = np.array([0.03, 0.06])
    regularize_scale = options['ap_regularize_scale'] if 'ap_regularize_scale' in options else 1.
    N_perturb = 5

    count = 0

    count_nochange = 0
    use_center = copy(results['center'])
    I = np.array(range(len(sample_radii)))
    while count < 300 and count_nochange < (3*len(sample_radii)):
        # Periodically include logging message
        if count % 10 == 0:
            logging.debug('%s: count: %i' % (options['ap_name'],count))
        count += 1
        
        np.random.shuffle(I)
        for i in I:
            perturbations = []
            perturbations.append({'ellip': copy(ellip), 'pa': copy(pa)})
            perturbations[-1]['loss'] = _FFT_mean_loss(dat, sample_radii, perturbations[-1]['ellip'], perturbations[-1]['pa'], i,
                                                       use_center, results['background noise'], mask = mask, reg_scale = regularize_scale if count > 4 else 0, name = options['ap_name'])
            for n in range(N_perturb):
                perturbations.append({'ellip': copy(ellip), 'pa': copy(pa)})
                if count % 3 in [0,1]:
                    perturbations[-1]['ellip'][i] = _x_to_eps(_inv_x_to_eps(perturbations[-1]['ellip'][i]) + np.random.normal(loc = 0, scale = perturb_scale[0]))
                if count % 3 in [1,2]:
                    perturbations[-1]['pa'][i] = (perturbations[-1]['pa'][i] + np.random.normal(loc = 0, scale = perturb_scale[1])) % np.pi
                perturbations[-1]['loss'] = _FFT_mean_loss(dat, sample_radii, perturbations[-1]['ellip'], perturbations[-1]['pa'], i,
                                                           use_center, results['background noise'], mask = mask, reg_scale = regularize_scale if count > 4 else 0, name = options['ap_name'])
            
            best = np.argmin(list(p['loss'] for p in perturbations))
            if best > 0:
                ellip = copy(perturbations[best]['ellip'])
                pa = copy(perturbations[best]['pa'])
                count_nochange = 0
            else:
                count_nochange += 1
                
    logging.info('%s: Completed isohpote fit in %i itterations' % (options['ap_name'], count))
    # detect collapsed center
    ######################################################################
    for i in range(5):
        if (_inv_x_to_eps(ellip[i]) - _inv_x_to_eps(ellip[i+1])) > 0.5:
            ellip[:i+1] = ellip[i+1]
            pa[:i+1] = pa[i+1]

    # Smooth ellip and pa profile
    ######################################################################
    smooth_ellip = copy(ellip)
    smooth_pa = copy(pa)
    ellip[:3] = min(ellip[:3])
    smooth_ellip = _ellip_smooth(sample_radii, smooth_ellip, 5)
    smooth_pa = _pa_smooth(sample_radii, smooth_pa, 5)
    
    if 'ap_doplot' in options and options['ap_doplot']:
        ranges = [[max(0,int(use_center['x']-sample_radii[-1]*1.2)), min(dat.shape[1],int(use_center['x']+sample_radii[-1]*1.2))],
                  [max(0,int(use_center['y']-sample_radii[-1]*1.2)), min(dat.shape[0],int(use_center['y']+sample_radii[-1]*1.2))]]
        LSBImage(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]], results['background noise'])
        # plt.imshow(np.clip(dat[ranges[1][0]: ranges[1][1], ranges[0][0]: ranges[0][1]],
        #                    a_min = 0,a_max = None), origin = 'lower', cmap = 'Greys', norm = ImageNormalize(stretch=LogStretch())) 
        for i in range(len(sample_radii)):
            plt.gca().add_patch(Ellipse((use_center['x'] - ranges[0][0],use_center['y'] - ranges[1][0]), 2*sample_radii[i], 2*sample_radii[i]*(1. - ellip[i]),
                                        pa[i]*180/np.pi, fill = False, linewidth = ((i+1)/len(sample_radii))**2, color = 'r'))
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sfit_ellipse_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi'in options else 300)
        plt.close()
        
        plt.scatter(sample_radii, ellip, color = 'r', label = 'ellip')
        plt.scatter(sample_radii, pa/np.pi, color = 'b', label = 'pa/$np.pi$')
        show_ellip = _ellip_smooth(sample_radii, ellip, deg = 5)
        show_pa = _pa_smooth(sample_radii, pa, deg = 5)
        plt.plot(sample_radii, show_ellip, color = 'orange', linewidth = 2, linestyle='--', label = 'smooth ellip')
        plt.plot(sample_radii, show_pa/np.pi, color = 'purple', linewidth = 2, linestyle='--', label = 'smooth pa/$np.pi$')
        #plt.xscale('log')
        plt.legend()
        if not ('ap_nologo' in options and options['ap_nologo']):
            AddLogo(plt.gcf())
        plt.savefig('%sphaseprofile_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi'in options else 300)
        plt.close()

    # Compute errors
    ######################################################################
    ellip_err = np.zeros(len(ellip))
    ellip_err[:2] = np.sqrt(np.sum((ellip[:4] - smooth_ellip[:4])**2)/4)
    ellip_err[-1] = np.sqrt(np.sum((ellip[-4:] - smooth_ellip[-4:])**2)/4)
    pa_err = np.zeros(len(pa))
    pa_err[:2] = np.sqrt(np.sum((pa[:4] - smooth_pa[:4])**2)/4)
    pa_err[-1] = np.sqrt(np.sum((pa[-4:] - smooth_pa[-4:])**2)/4)
    for i in range(2,len(pa)-1):
        ellip_err[i] = np.sqrt(np.sum((ellip[i-2:i+2] - smooth_ellip[i-2:i+2])**2)/4)
        pa_err[i] = np.sqrt(np.sum((pa[i-2:i+2] - smooth_pa[i-2:i+2])**2)/4)

    res = {'fit ellip': ellip, 'fit pa': pa, 'fit R': sample_radii,
           'fit ellip_err': ellip_err, 'fit pa_err': pa_err,
           'auxfile fitlimit': 'fit limit semi-major axis: %.2f pix' % sample_radii[-1]}
    return IMG, res
