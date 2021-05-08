import numpy as np
import matplotlib.pyplot as plt
import sys
import os
sys.path.append(os.environ['AUTOPROF'])
from autoprofutils.SharedFunctions import _iso_extract, _x_to_eps, _x_to_pa, _inv_x_to_pa, _inv_x_to_eps, LSBImage, Angle_Average, Angle_Median, AddLogo, PA_shift_convention, Sigma_Clip_Upper
import logging

def Plot_Galaxy_Image(IMG, results, options):
    """
    Plots an LSB image of the object without anything else drawn above it.
    Useful for inspecting images for spurious features
    """
    
    if 'center' in results:
        center = results['center']
    elif 'ap_set_center' in options:
        center = options['ap_set_center']
    elif 'ap_guess_center' in options:
        center = options['ap_guess_center']
    else:
        center = {'x': IMG.shape[1]/2, 'y': IMG.shape[0]/2}

    if 'prof data' in results:
        edge = 1.2*results['prof data']['R'][-1]/options['ap_pixscale']
    elif 'init R' in results:
        edge = 3*results['init R']
    elif 'fit R' in results:
        edge = 2*results['fit R']
    else:
        edge = max(IMG.shape)/2
    edge = min([edge, abs(center['x'] - IMG.shape[1]), center['x'], abs(center['y'] - IMG.shape[0]), center['y']])
        
    ranges = [[max(0,int(center['x']-edge)), min(IMG.shape[1],int(center['x']+edge))],
              [max(0,int(center['y']-edge)), min(IMG.shape[0],int(center['y']+edge))]]
        
    LSBImage(IMG[ranges[1][0]:ranges[1][1],ranges[0][0]:ranges[0][1]] - results['background'], results['background noise'])
    if not ('ap_nologo' in options and options['ap_nologo']):
        AddLogo(plt.gcf())
    plt.savefig('%sclean_image_%s.jpg' % (options['ap_plotpath'] if 'ap_plotpath' in options else '', options['ap_name']), dpi = options['ap_plotdpi'] if 'ap_plotdpi' in options else 300)
    plt.close()
    
    return IMG, {}
