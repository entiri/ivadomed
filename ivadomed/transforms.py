import math
import random
import numpy as np
from PIL import Image
import torch
import torchvision.transforms.functional as F
from scipy.ndimage.measurements import label, center_of_mass

from scipy.ndimage.morphology import binary_dilation, binary_fill_holes, binary_closing
from medicaltorch import transforms as mt_transforms

from torchvision import transforms as torchvision_transforms



class IMEDTransform(object):

    def __call__(self, sample):
        raise NotImplementedError("You need to implement the transform() method.")

    def undo_transform(self, sample):
        raise NotImplementedError("You need to implement the undo_transform() method.")


class UndoCompose(object):
    def __init__(self, compose):
        self.transforms = compose.transforms

    def __call__(self, img):
        for t in self.transforms[::-1]:
            img = t.undo_transform(img)
        return img


class UndoTransform(object):
    def __init__(self, transform):
        self.transform = transform

    def __call__(self, sample):
        return self.transform.undo_transform(sample)


class ToPIL(MTTransform):
    """Converts array or tensor to PIL object."""

    def __init__(self, labeled=True):
        self.labeled = labeled

    def sample_transform(self, sample_data):
        # Numpy array
        if not isinstance(sample_data, np.ndarray):
            input_data_npy = sample_data.numpy()
        else:
            input_data_npy = sample_data

        input_data = Image.fromarray(input_data_npy, mode='F')
        return input_data

    def __call__(self, sample):
        rdict = {}

        # Input data
        input_data = sample['input']
        ret_input = [self.sample_transform(item) for item in input_data]
        rdict['input'] = ret_input

        # Labeled data
        if self.labeled:
            gt_data = sample['gt']
            ret_gt = [self.sample_transform(item) for item in gt_data]
            rdict['gt'] = ret_gt

        sample.update(rdict)
        return sample


def get_transform_names():
    """Function used in the main to differentiate the IVADO transfroms
       from the mt_transforms."""

    return ['DilateGT', 'ROICrop2D', 'Resample', 'NormalizeInstance', 'ToTensor', 'StackTensors', 'CenterCrop3D',
            'RandomAffine3D', 'NormalizeInstance3D', 'ToTensor3D', 'BackgroundClass']


def compose_transforms(dict_transforms, requires_undo=False):
    """Composes several transforms together.
    Args:
        dict_transforms (dictionary): Dictionary where the keys are the transform names
            and the value their parameters.
        requires_undo (bool): If True, does not include transforms which do not have an undo_transform
            implemented yet.
    Returns:
        torchvision.transforms.Compose object.
    """
    list_transform = []
    for transform in dict_transforms.keys():
        parameters = dict_transforms[transform]

        # call transfrom either from ivadomed either from medicaltorch
        if transform in get_transform_names():
            transform_obj = globals()[transform](**parameters)
        else:
            transform_obj = getattr(mt_transforms, transform)(**parameters)

        # check if undo_transform method is implemented
        if requires_undo:
            if hasattr(transform_obj, 'undo_transform'):
                list_transform.append(transform_obj)
            else:
                print('{} transform not included since no undo_transform available for it.'.format(transform))
        else:
            list_transform.append(transform_obj)

    return torchvision_transforms.Compose(list_transform)


class Resample(mt_transforms.Resample):
    """This class extends mt_transforms.Resample:
        resample the ROI image if provided."""

    def __init__(self, wspace, hspace,
                 interpolation=Image.BILINEAR,
                 labeled=True):
        super().__init__(wspace, hspace, interpolation, labeled)

    def resample_bin(self, data, wshape, hshape, thr=0.5):
        data = data.resize((wshape, hshape), resample=Image.NEAREST)
        return data

    def undo_transform(self, sample):
        rdict = {
            "input": [],
            "gt": []
        }

        for input_data in sample["input"]:
            # undo image
            hshape, wshape = sample['input_metadata']['data_shape']
            hzoom, wzoom = sample['input_metadata']['zooms']
            input_data_undo = input_data.resize((wshape, hshape),
                                                     resample=self.interpolation)
            rdict['input'].append(input_data_undo)

        # undo pred, aka GT
        # CG: I comment these 2 following lines because these variables should be the
        # same between image and GT
        #hshape, wshape = sample['gt_metadata']['data_shape']
        #hzoom, wzoom = sample['gt_metadata']['zooms']

        for gt in sample["gt"]:
            gt_data_undo = self.resample_bin(gt, wshape, hshape)
            rdict['gt'].append(gt_data_undo)

        sample.update(rdict)
        return sample

    def __call__(self, sample):
        rdict = {}
        input_data = sample['input']
        input_metadata = sample['input_metadata']

        # Based on the assumption that the metadata of every modality are equal.
        # Voxel dimension in mm
        hzoom, wzoom = input_metadata[0]["zooms"]
        hshape, wshape = input_metadata[0]["data_shape"]

        hfactor = hzoom / self.hspace
        wfactor = wzoom / self.wspace

        hshape_new = int(round(hshape * hfactor))
        wshape_new = int(round(wshape * wfactor))
        
        for i, input_image in enumerate(input_data):
            input_data[i] = input_image.resize((wshape_new, hshape_new),
                                                  resample=self.interpolation)


        for i, input_image in enumerate(input_data):
            input_data[i] = input_image.resize((wshape_new, hshape_new),
                                               resample=self.interpolation)

        rdict['input'] = input_data

        if self.labeled:
            gt_data = sample['gt']
            rdict['gt'] = []
            for gt in gt_data:
                rdict['gt'].append(self.resample_bin(gt, wshape_new, hshape_new))

        if sample['roi'] is not None:
            roi_data = sample['roi']
            rdict['roi'] = []
            for roi in roi_data:
                rdict['roi'].append(self.resample_bin(roi, wshape_new, hshape_new))

        sample.update(rdict)
        return sample


class NormalizeInstance(mt_transforms.NormalizeInstance):
    """This class extends mt_transforms.Normalize"""

    def undo_transform(self, sample):
        return sample


class ToTensor(IMEDTransform):
    """Convert a PIL image(s) or numpy array(s) to a PyTorch tensor(s)."""

    def __init__(self, labeled=True):
        self.labeled = labeled

    def __call__(self, sample):
        rdict = {}
        input_data = sample['input']

        # TODO: function?
        # Input data
        if len(input_data) > 1:
            # Multiple inputs
            ret_input = [F.to_tensor(item) for item in input_data]
        else:
            # single input
            ret_input = F.to_tensor(input_data[0])
        rdict['input'] = ret_input

        # Labeled data
        if self.labeled:
            gt_data = sample['gt']
            if gt_data is not None:
                if isinstance(gt_data, list):
                    # TODO: comment
                    # Add dim 0 for 3D images
                    if gt_data[0].size == 3:
                        ret_gt = [gt.unsqueeze(0) for gt in sample['gt']]

                    # multiple GT
                    # torch.cat is used to be compatible with StackTensors
                    ret_gt = torch.cat([F.to_tensor(item) for item in gt_data], dim=0)

                else:
                    # single GT
                    ret_gt = F.to_tensor(gt_data)

                rdict['gt'] = ret_gt

        # Update sample
        sample.update(rdict)
        return sample

    def undo_transform(self, sample):
        # Returns a PIL object
        return mt_transforms.ToPIL()(sample)


class ROICrop2D(mt_transforms.CenterCrop2D):
    """Make a crop of a specified size around a ROI.
    :param labeled: if it is a segmentation task.
                         When this is True (default), the crop
                         will also be applied to the ground truth.
    """

    def __init__(self, size, labeled=True):
        super().__init__(size, labeled)

    def _uncrop(self, data, params):
        fh, fw, w, h = params
        tw, th = self.size
        pad_left = fw
        pad_right = w - pad_left - tw
        pad_top = fh
        pad_bottom = h - pad_top - th
        padding = (pad_top, pad_left, pad_bottom, pad_right)
        return F.pad(data, padding)

    def undo_transform(self, sample):
        rdict = {
            'input': [],
            'gt': []
        }

        if isinstance(sample['input'], list):
            for input_data in sample['input']:
                rdict['input'].append(self._uncrop(input_data, sample['input_metadata']["__centercrop"]))

        #if self.labeled:
        for gt in sample['gt']:
            rdict['gt'].append(self._uncrop(gt, sample['input_metadata']["__centercrop"]))

        sample.update(rdict)
        return sample

    def __call__(self, sample):
        rdict = {}

        input_data = sample['input']
        roi_data = sample['roi'][0]

        # compute center of mass of the ROI
        x_roi, y_roi = center_of_mass(np.array(roi_data).astype(np.int))
        x_roi, y_roi = int(round(x_roi)), int(round(y_roi))
        tw, th = self.size
        th_half, tw_half = int(round(th / 2.)), int(round(tw / 2.))

        # compute top left corner of the crop area
        fh = y_roi - th_half
        fw = x_roi - tw_half

        for i in range(len(input_data)):
            w, h = input_data[i].size
            params = (fh, fw, h, w)

            self.propagate_params(sample, params, i)
            # crop data
            input_data[i] = F.crop(input_data[i], fw, fh, tw, th)

        rdict['input'] = input_data
        if self.labeled:
            gt_data = sample['gt']
            gt_metadata = sample['gt_metadata']
            for i in range(len(gt_data)):
                gt_data[i] = F.crop(gt_data[i], fw, fh, tw, th)
                gt_metadata[i]["__centercrop"] = (fh, fw, h, w)
            rdict['gt'] = gt_data
            rdict['gt_metadata'] = gt_metadata

        sample.update(rdict)
        return sample


class DilateGT(IMEDTTransform):
    """Randomly dilate a tensor ground-truth.
    :param dilation_factor: float, controls the number of dilation iterations.
                            For each individual lesion, the number of dilation iterations is computed as follows:
                                nb_it = int(round(dilation_factor * sqrt(lesion_area)))
                            If dilation_factor <= 0, then no dilation will be perfomed.
    """

    def __init__(self, dilation_factor):
        self.dil_factor = dilation_factor

    def dilate_lesion(self, arr_bin, arr_soft, label_values):
        for lb in label_values:
            # binary dilation with 1 iteration
            arr_dilated = binary_dilation(arr_bin, iterations=1)

            # isolate new voxels, i.e. the ones from the dilation
            new_voxels = np.logical_xor(arr_dilated, arr_bin).astype(np.int)

            # assign a soft value (]0, 1[) to the new voxels
            soft_new_voxels = lb * new_voxels

            # add the new voxels to the input mask
            arr_soft += soft_new_voxels
            arr_bin = (arr_soft > 0).astype(np.int)

        return arr_bin, arr_soft

    def dilate_arr(self, arr, dil_factor):
        # identify each object
        arr_labeled, lb_nb = label(arr.astype(np.int))

        # loop across each object
        arr_bin_lst, arr_soft_lst = [], []
        for obj_idx in range(1, lb_nb + 1):
            arr_bin_obj = (arr_labeled == obj_idx).astype(np.int)
            arr_soft_obj = np.copy(arr_bin_obj).astype(np.float)
            # compute the number of dilation iterations depending on the size of the lesion
            nb_it = int(round(dil_factor * math.sqrt(arr_bin_obj.sum())))
            # values of the voxels added to the input mask
            soft_label_values = [x / (nb_it + 1) for x in range(nb_it, 0, -1)]
            # dilate lesion
            arr_bin_dil, arr_soft_dil = self.dilate_lesion(arr_bin_obj, arr_soft_obj, soft_label_values)
            arr_bin_lst.append(arr_bin_dil)
            arr_soft_lst.append(arr_soft_dil)

        # sum dilated objects
        arr_bin_idx = np.sum(np.array(arr_bin_lst), axis=0)
        arr_soft_idx = np.sum(np.array(arr_soft_lst), axis=0)
        # clip values in case dilated voxels overlap
        arr_bin_clip, arr_soft_clip = np.clip(arr_bin_idx, 0, 1), np.clip(arr_soft_idx, 0.0, 1.0)

        return arr_soft_clip.astype(np.float), arr_bin_clip.astype(np.int)

    def random_holes(self, arr_in, arr_soft, arr_bin):
        arr_soft_out = np.copy(arr_soft)

        # coordinates of the new voxels, i.e. the ones from the dilation
        new_voxels_xx, new_voxels_yy = np.where(np.logical_xor(arr_bin, arr_in))
        nb_new_voxels = new_voxels_xx.shape[0]

        # ratio of voxels added to the input mask from the dilated mask
        new_voxel_ratio = random.random()
        # randomly select new voxel indexes to remove
        idx_to_remove = random.sample(range(nb_new_voxels),
                                      int(round(nb_new_voxels * (1 - new_voxel_ratio))))

        # set to zero the here-above randomly selected new voxels
        arr_soft_out[new_voxels_xx[idx_to_remove], new_voxels_yy[idx_to_remove]] = 0.0
        arr_bin_out = (arr_soft_out > 0).astype(np.int)

        return arr_soft_out, arr_bin_out

    def post_processing(self, arr_in, arr_soft, arr_bin, arr_dil):
        # remove new object that are not connected to the input mask
        arr_labeled, lb_nb = label(arr_bin)
        connected_to_in = arr_labeled * arr_in
        for lb in range(1, lb_nb + 1):
            if np.sum(connected_to_in == lb) == 0:
                arr_soft[arr_labeled == lb] = 0

        # binary closing
        arr_bin_closed = binary_closing((arr_soft > 0).astype(bool))
        # fill binary holes
        arr_bin_filled = binary_fill_holes(arr_bin_closed)

        # recover the soft-value assigned to the filled-holes
        arr_soft_out = arr_bin_filled * arr_dil

        return arr_soft_out

    def __call__(self, sample):
        gt_data = sample['gt']
        gt_t = []
        for gt in gt_data:
            gt_data_np = np.array(gt)
            # binarize for processing
            gt_data_np = (gt_data_np > 0.5).astype(np.int_)

            if self.dil_factor > 0 and np.sum(gt):
                # dilation
                gt_dil, gt_dil_bin = self.dilate_arr(gt_data_np, self.dil_factor)

                # random holes in dilated area
                gt_holes, gt_holes_bin = self.random_holes(gt_data_np, gt_dil, gt_dil_bin)

                # post-processing
                gt_pp = self.post_processing(gt_data_np, gt_holes, gt_holes_bin, gt_dil)

                # mask with ROI
                if sample['roi'][0] is not None:
                    gt_pp[np.array(sample['roi'][0]) == 0] = 0.0

                gt_t.append(Image.fromarray(gt_pp))

        if len(gt_t):
            rdict = {
                'gt': gt_t,
            }
            sample.update(rdict)

        return sample


class StackTensors(mt_transforms.StackTensors):
    """This class extends mt_transforms.NormalizeInstance"""
    def undo_transform(self, sample):
        return sample


# 3D Transforms
class CenterCrop3D(IMEDTransform):
    """Make a centered crop of a specified size.
    :param labeled: if it is a segmentation task.
                         When this is True (default), the crop
                         will also be applied to the ground truth.
    """

    def __init__(self, size, labeled=True):
        self.size = size
        self.labeled = labeled

    def _uncrop(self, sample):
        td, tw, th = sample['input_metadata']["__centercrop"]
        d, w, h = sample['input_metadata']["data_shape"]
        fh = max(int(round((h - th) / 2.)), 0)
        fw = max(int(round((w - tw) / 2.)), 0)
        fd = max(int(round((d - td) / 2.)), 0)
        npad = ((0, 0), (fw, fw), (fd, fd), (fh, fh))
        sample['input'] = np.pad(sample['input'], pad_width=npad, mode='constant', constant_values=0)

        #if self.labeled:
        sample['gt'] = np.pad(sample['gt'], pad_width=npad, mode='constant', constant_values=0)

        return sample

    def undo_transform(self, sample):
        rdict = self._uncrop(sample)
        sample.update(rdict)
        return sample

    def __call__(self, input_data):
        for idx, input_volume in enumerate(input_data['input']):
            gt_img = input_data['gt']
            input_img = input_volume
            d, w, h = gt_img[0].shape
            td, tw, th = self.size
            fh = max(int(round((h - th) / 2.)), 0)
            fw = max(int(round((w - tw) / 2.)), 0)
            fd = max(int(round((d - td) / 2.)), 0)
            if self.labeled:
                gt_img = input_data['gt']
                crop_gt = []
                for gt in gt_img:
                    crop_gt.append(gt[fd:fd + td, fw:fw + tw, fh:fh + th])
            crop_input = input_img[fd:fd + td, fw:fw + tw, fh:fh + th]
            # Pad image with mean if image smaller than crop size
            cd, cw, ch = crop_input.shape
            if (cw, ch, cd) != (tw, th, td):
                w_diff = (tw - cw) / 2.
                iw = 1 if w_diff % 1 != 0 else 0
                h_diff = (th - ch) / 2.
                ih = 1 if h_diff % 1 != 0 else 0
                d_diff = (td - cd) / 2.
                id = 1 if d_diff % 1 != 0 else 0
                npad = ((int(d_diff) + id, int(d_diff)),
                        (int(w_diff) + iw, int(w_diff)),
                        (int(h_diff) + ih, int(h_diff)))
                crop_input = np.pad(crop_input, pad_width=npad, mode='constant', constant_values=np.mean(crop_input))
                if self.labeled:
                    for i, gt in enumerate(crop_gt):
                        crop_gt[i] = np.pad(gt, pad_width=npad, mode='constant', constant_values=0)
            input_data['input'][idx] = crop_input

            if self.labeled:
                input_data['gt'] = crop_gt

            input_data['input_metadata'][idx]["__centercrop"] = td, tw, th

        return input_data


class NormalizeInstance3D(mt_transforms.NormalizeInstance3D):
    """This class extends mt_transforms.NormalizeInstance"""

    @staticmethod
    def undo_transform(sample):
        return sample


class BackgroundClass(IMEDTransform):
    def undo_transform(self, sample):
        return sample

    def __call__(self, sample):
        rdict = {}

        background = (sample['gt'].sum(axis=0) == 0).type('torch.FloatTensor')[None, ]
        rdict['gt'] = torch.cat((background, sample['gt']), dim=0)
        sample.update(rdict)
        return sample


class RandomAffine3D(mt_transforms.RandomAffine):
    def __call__(self, sample):
        """This class extends mt_transforms.RandomAffine"""

        rdict = {}
        input_data = sample['input']
        if not isinstance(input_data, list):
            input_data = [sample['input']]

        input_data_size = input_data[0][0, :, :].shape
        params = self.get_params(self.degrees, self.translate, self.scale,
                                 self.shear, input_data_size)
        ret_input = []
        for volume in input_data:
            img_data = np.zeros(input_data[0].shape)
            for idx, img in enumerate(volume):
                pil_img = Image.fromarray(img, mode='F')
                img_data[idx, :, :] = np.array(self.sample_augment(pil_img, params))
            ret_input.append(img_data.astype('float32'))

        rdict['input'] = ret_input

        if self.labeled:
            gt_data = sample['gt']
            ret_gt = []
            for labels in gt_data:
                gt_vol = np.zeros(labels.shape)
                for idx, gt in enumerate(labels):
                    pil_img = Image.fromarray(gt, mode='F')
                    gt_vol[idx, :, :] = np.array(self.sample_augment(pil_img, params))
                ret_gt.append(gt_vol.astype('float32'))
            rdict['gt'] = ret_gt

        sample.update(rdict)
        return sample


class ToTensor3D(mt_transforms.ToTensor):
    """This class extends mt_transforms.ToTensor"""

    def undo_transform(self, sample):
        rdict = {}
        rdict['input'] = np.array(sample['input'])
        #if self.labeled:
        rdict['gt'] = np.array(sample['gt'])

        sample.update(rdict)
        return sample
