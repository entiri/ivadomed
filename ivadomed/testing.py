import os

import numpy as np
import torch
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from tqdm import tqdm

from ivadomed import metrics as imed_metrics
from ivadomed import utils as imed_utils
from ivadomed.training import get_metadata
from ivadomed.loader import utils as imed_loader_utils

cudnn.benchmark = True


def test(model_params, dataset_test, testing_params, log_directory, device, cuda_available=True,
         metric_fns=None):
    """Main command to test the network.

    Args:
        model_params (dict): Model's parameters.
        dataset_test (imed_loader): Testing dataset
        testing_params (dict):
        log_directory (string):
        device (torch.device):
        cuda_available (Bool):
        metric_fns (list):
        debugging (Bool):
    Returns:
        dict: result metrics
    """
    # DATA LOADER
    test_loader = DataLoader(dataset_test, batch_size=testing_params["batch_size"],
                             shuffle=False, pin_memory=True,
                             collate_fn=imed_loader_utils.imed_collate,
                             num_workers=0)

    # LOAD TRAIN MODEL
    fname_model = os.path.join(log_directory, "best_model.pt")
    print('\nLoading model: {}'.format(fname_model))
    model = torch.load(fname_model, map_location=device)
    if cuda_available:
        model.cuda()
    model.eval()

    # CREATE OUTPUT FOLDER
    path_3Dpred = os.path.join(log_directory, 'pred_masks')
    if not os.path.isdir(path_3Dpred):
        os.makedirs(path_3Dpred)

    # METRIC MANAGER
    metric_mgr = imed_metrics.MetricManager(metric_fns)

    # UNCERTAINTY SETTINGS
    if (testing_params['uncertainty']['epistemic'] or testing_params['uncertainty']['aleatoric']) and \
            testing_params['uncertainty']['n_it'] > 0:
        n_monteCarlo = testing_params['uncertainty']['n_it']
        testing_params['uncertainty']['applied'] = True
        print('\nComputing model uncertainty over {} iterations.'.format(n_monteCarlo))
    else:
        testing_params['uncertainty']['applied'] = False
        n_monteCarlo = 1

    for i_monteCarlo in range(n_monteCarlo):
        preds_npy, gt_npy = run_inference(test_loader, model, model_params, testing_params, path_3Dpred,
                                          cuda_available, i_monteCarlo, log_directory)
        metric_mgr(preds_npy, gt_npy)

    # COMPUTE UNCERTAINTY MAPS
    if n_monteCarlo > 1:
        imed_utils.run_uncertainty(ifolder=path_3Dpred)

    metrics_dict = metric_mgr.get_results()
    metric_mgr.reset()
    print(metrics_dict)
    return metrics_dict


def run_inference(test_loader, model, model_params, testing_params, ofolder, cuda_available,
                  i_monteCarlo=None, log_directory=None):
    """Run inference on the test data and save results as nibabel files.

    Args:
        test_loader (torch DataLoader):
        model (nn.Module):
        model_params (dict):
        testing_params (dict):
        ofolder (string): Where the nibabel files are saved
        device (torch.device):
        cuda_available (Bool):
        i_monteCarlo (int): i_th Monte Carlo iteration
    Returns:
        np.array, np.array: pred, gt of shape n_sample, n_label, h, w, d
    """
    # INIT STORAGE VARIABLES
    preds_npy_list, gt_npy_list = [], []
    pred_tmp_lst, z_tmp_lst, fname_tmp = [], [], ''
    # LOOP ACROSS DATASET
    for i, batch in enumerate(tqdm(test_loader, desc="Inference - Iteration " + str(i_monteCarlo))):
        with torch.no_grad():
            # GET SAMPLES
            # input_samples: list of batch_size tensors, whose size is n_channels X height X width X depth
            # gt_samples: idem with n_labels
            # batch['*_metadata']: list of batch_size lists, whose size is n_channels or n_labels
            if model_params["name"] == "HeMISUnet":
                input_samples = imed_utils.cuda(imed_utils.unstack_tensors(batch["input"]), cuda_available)
            else:
                input_samples = imed_utils.cuda(batch["input"], cuda_available)
            gt_samples = imed_utils.cuda(batch["gt"], cuda_available, non_blocking=True)

            # EPISTEMIC UNCERTAINTY
            if testing_params['uncertainty']['applied'] and testing_params['uncertainty']['epistemic']:
                for m in model.modules():
                    if m.__class__.__name__.startswith('Dropout'):
                        m.train()

            # RUN MODEL
            if model_params["name"] in ["HeMISUnet", "FiLMedUnet"]:
                metadata = get_metadata(batch["input_metadata"], model_params)
                preds = model(input_samples, metadata)
            else:
                preds = model(input_samples)

        if model_params["name"] == "HeMISUnet":
            # Reconstruct image with only one modality
            input_samples = batch['input'][0]

        if model_params["name"] == "UNet3D" and model_params["attention"]:
            imed_utils.save_feature_map(batch, "attentionblock2", log_directory, model, input_samples,
                                        slice_axis=test_loader.dataset.slice_axis)

        # PREDS TO CPU
        preds_cpu = preds.cpu()
        gt_npy_list.append(gt_samples.cpu().numpy().astype(np.uint8))
        preds_npy_list.append(preds_cpu.data.numpy().astype(np.uint8))

        # RECONSTRUCT 3D IMAGE
        last_batch_bool = (i == len(test_loader) - 1)
        # LOOP ACROSS SAMPLES
        for smp_idx in range(len(preds_cpu)):
            last_sample_bool = (last_batch_bool and smp_idx == len(batch) - 1)
            # undo transformations
            preds_idx_undo, metadata_idx = testing_params["undo_transforms"](preds_cpu[smp_idx],
                                                                             batch['gt_metadata'][smp_idx],
                                                                             data_type='gt')
            # preds_idx_undo is a list n_label arrays
            preds_idx_arr = np.array(preds_idx_undo)

            # TODO: gt_filenames should not be a list
            fname_ref = metadata_idx[0]['gt_filenames'][0]

            if not model_params["name"].endswith('3D'):
                # NEW COMPLETE VOLUME
                if pred_tmp_lst and (fname_ref != fname_tmp or last_sample_bool):
                    # save the completely processed file as a nifti file
                    fname_pred = os.path.join(ofolder, fname_tmp.split('/')[-1])
                    fname_pred = fname_pred.split(testing_params['target_suffix'][0])[0] + '_pred.nii.gz'
                    # If Uncertainty running, then we save each simulation result
                    if testing_params['uncertainty']['applied']:
                        fname_pred = fname_pred.split('.nii.gz')[0] + '_' + str(i_monteCarlo).zfill(2) + '.nii.gz'

                    output_nii = imed_utils.pred_to_nib(data_lst=pred_tmp_lst,
                                                        z_lst=z_tmp_lst,
                                                        fname_ref=fname_tmp,
                                                        fname_out=fname_pred,
                                                        slice_axis=imed_utils.AXIS_DCT[testing_params['slice_axis']],
                                                        kernel_dim='2d',
                                                        bin_thr=0.5 if testing_params["binarize_prediction"] else -1)

                    output_nii_shape = output_nii.get_fdata().shape
                    if len(output_nii_shape) == 4 and output_nii_shape[0] > 1:
                        imed_utils.save_color_labels(output_nii.get_fdata(),
                                                     testing_params["binarize_prediction"],
                                                     fname_tmp,
                                                     fname_pred.split(".nii.gz")[0] + '_color.nii.gz',
                                                     imed_utils.AXIS_DCT[testing_params['slice_axis']])

                    # re-init pred_stack_lst
                    pred_tmp_lst, z_tmp_lst = [], []

                # add new sample to pred_tmp_lst, of size n_label X h X w ...
                pred_tmp_lst.append(preds_idx_arr)

                # TODO: slice_index should be stored in gt_metadata as well
                z_tmp_lst.append(int(batch['input_metadata'][smp_idx][0]['slice_index']))
                fname_tmp = fname_ref

            else:
                # TODO: Add reconstruction for subvolumes
                fname_pred = os.path.join(ofolder, fname_ref.split('/')[-1])
                fname_pred = fname_pred.split(testing_params['target_suffix'][0])[0] + '_pred.nii.gz'
                # If uncertainty running, then we save each simulation result
                if testing_params['uncertainty']['applied']:
                    fname_pred = fname_pred.split('.nii.gz')[0] + '_' + str(i_monteCarlo).zfill(2) + '.nii.gz'

                # Choose only one modality
                imed_utils.pred_to_nib(data_lst=[preds_idx_arr],
                                       z_lst=[],
                                       fname_ref=fname_ref,
                                       fname_out=fname_pred,
                                       slice_axis=imed_utils.AXIS_DCT[testing_params['slice_axis']],
                                       kernel_dim='3d',
                                       bin_thr=0.5 if testing_params["binarize_prediction"] else -1)

                # Save merged labels with color
                if preds_idx_arr.shape[0] > 1:
                    imed_utils.save_color_labels(preds_idx_arr,
                                                 testing_params['binarize_prediction'],
                                                 batch['input_metadata'][smp_idx][0]['input_filenames'],
                                                 fname_pred.split(".nii.gz")[0] + '_color.nii.gz',
                                                 imed_utils.AXIS_DCT[testing_params['slice_axis']])

    return np.concatenate(preds_npy_list, axis=0), np.concatenate(gt_npy_list, axis=0)