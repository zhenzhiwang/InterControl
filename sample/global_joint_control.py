# This code is based on https://github.com/openai/guided-diffusion
"""
Generate a large batch of image samples from a model and save them as a large
numpy array. This can be used to produce samples for FID evaluation.
"""
from diffusion.control_diffusion import ControlGaussianDiffusion
from diffusion.respace import SpacedDiffusion
from utils.fixseed import fixseed
import os
import numpy as np
import torch
from utils.parser_util import edit_control_args
from utils.model_util import load_controlmdm_and_diffusion
from utils import dist_util
from model.cfg_sampler import wrap_model
from data_loaders.get_data import get_dataset_loader
from data_loaders.humanml.scripts.motion_process import recover_from_ric
from data_loaders.humanml_utils import get_control_mask, HML_JOINT_NAMES
import data_loaders.humanml.utils.paramUtil as paramUtil
from data_loaders.humanml.utils.plot_script import plot_3d_motion
import shutil
from model.ControlMDM import ControlMDM

def main():
    args = edit_control_args()
    assert args.multi_person == False, 'multi-person is not supported for this script'
    fixseed(args.seed)
    out_path = args.output_dir
    name = os.path.basename(os.path.dirname(args.model_path))
    niter = os.path.basename(args.model_path).replace('model', '').replace('.pt', '')
    max_frames = 196 if args.dataset in ['kit', 'humanml'] else 60
    fps = 12.5 if args.dataset == 'kit' else 20
    dist_util.setup_dist(args.device)
    if out_path == '':
        out_path = os.path.join(os.path.dirname(args.model_path),
                                'edit_{}_{}_{}_seed{}'.format(name, niter, args.inpainting_mask, args.seed))
        if args.text_condition != '':
            out_path += '_' + args.text_condition.replace(' ', '_').replace('.', '')

    print('Loading dataset...')
    assert args.num_samples <= args.batch_size, \
        f'Please either increase batch_size({args.batch_size}) or reduce num_samples({args.num_samples})'
    # So why do we need this check? In order to protect GPU from a memory overload in the following line.
    # If your GPU can handle batch size larger then default, you can specify it through --batch_size flag.
    # If it doesn't, and you still want to sample more prompts, run this script with different seeds
    # (specify through the --seed flag)
    args.batch_size = args.num_samples  # Sampling a single batch from the testset, with exactly args.num_samples
    data = get_dataset_loader(name=args.dataset,
                              batch_size=args.batch_size,
                              num_frames=max_frames,
                              split='test',
                              load_mode='train',
                              size=args.num_samples)  # in train mode, you get both text and motion.
    # data.fixed_length = n_frames
    total_num_samples = args.num_samples * args.num_repetitions

    print("Creating model and diffusion...")
    DiffusionClass = ControlGaussianDiffusion if args.filter_noise else SpacedDiffusion
    model, diffusion = load_controlmdm_and_diffusion(args, data, dist_util.dev(), ModelClass=ControlMDM, DiffusionClass=DiffusionClass)
    diffusion.mean = data.dataset.t2m_dataset.mean
    diffusion.std = data.dataset.t2m_dataset.std
    
     
    iterator = iter(data)
    input_motions, model_kwargs = next(iterator)
    input_motions = input_motions.to(dist_util.dev())
    if args.text_condition != '':
        texts = [args.text_condition] * args.num_samples
        model_kwargs['y']['text'] = texts

    # add inpainting mask according to args
    control_joint = 'right_wrist'
    assert max_frames == input_motions.shape[-1]
    gt_frames_per_sample = {}
    n_joints = 22 if input_motions.shape[1] == 263 else 21
    unnormalized_motion = data.dataset.t2m_dataset.inv_transform_torch(input_motions.permute(0, 2, 3, 1)).float()
    global_joints = recover_from_ric(unnormalized_motion, n_joints)
    global_joints = global_joints.view(-1, *global_joints.shape[2:]).permute(0, 2, 3, 1)
    global_joints.requires_grad = False
    model_kwargs['y']['global_joint'] = global_joints
    model_kwargs['y']['global_joint_mask'] = torch.tensor(get_control_mask(args.inpainting_mask, global_joints.shape, joint = control_joint, ratio=args.mask_ratio, dataset = args.dataset)).float().to(dist_util.dev())

    all_motions = []
    all_lengths = []
    all_text = []
    all_guidance = {'mask':[], 'joint':[]}

    for rep_i in range(args.num_repetitions):
        print(f'### Start sampling [repetitions #{rep_i}]')

        # add CFG scale to batch
        model_kwargs['y']['scale'] = torch.ones(args.batch_size, device=dist_util.dev()) * args.guidance_param

        sample_fn = diffusion.p_sample_loop

        sample = sample_fn(
            model,
            (args.batch_size, model.njoints, model.nfeats, max_frames),
            clip_denoised=False,
            model_kwargs=model_kwargs,
            skip_timesteps=0,  # 0 is the default value - i.e. don't skip any step
            init_image=None,
            progress=True,
            dump_steps=None,
            noise=None,
            const_noise=False,
            use_posterior = args.use_posterior,
        )

         
        # Recover XYZ *positions* from HumanML3D vector representation
        if model.data_rep == 'hml_vec':
            n_joints = 22 if sample.shape[1] == 263 else 21
            sample = data.dataset.t2m_dataset.inv_transform(sample.cpu().permute(0, 2, 3, 1)).float()
            sample = recover_from_ric(sample, n_joints)
            sample = sample.view(-1, *sample.shape[2:]).permute(0, 2, 3, 1)
             

        all_text += model_kwargs['y']['text']
        all_motions.append(sample.cpu().numpy())
        all_guidance['mask'].append(model_kwargs['y']['global_joint_mask'].cpu().numpy())
        all_guidance['joint'].append(model_kwargs['y']['global_joint'].cpu().numpy()) # [bs, njoints, 3, seqlen]
        all_lengths.append(model_kwargs['y']['lengths'].cpu().numpy())

        print(f"created {len(all_motions) * args.batch_size} samples")


    all_motions = np.concatenate(all_motions, axis=0)
    all_motions = all_motions[:total_num_samples]  # [bs, njoints, 3, seqlen]
    all_guidance['mask'] = np.concatenate(all_guidance['mask'], axis=0)[:total_num_samples]
    all_guidance['joint'] = np.concatenate(all_guidance['joint'], axis=0)[:total_num_samples]
    all_text = all_text[:total_num_samples]
    all_lengths = np.concatenate(all_lengths, axis=0)[:total_num_samples]
     
    if os.path.exists(out_path):
        shutil.rmtree(out_path)
    os.makedirs(out_path)

    npy_path = os.path.join(out_path, 'results.npy')
    print(f"saving results file to [{npy_path}]")
    np.save(npy_path,
            {'motion': all_motions, 'text': all_text, 'lengths': all_lengths,
             'num_samples': args.num_samples, 'num_repetitions': args.num_repetitions})
    with open(npy_path.replace('.npy', '.txt'), 'w') as fw:
        fw.write('\n'.join(all_text))
    with open(npy_path.replace('.npy', '_len.txt'), 'w') as fw:
        fw.write('\n'.join([str(l) for l in all_lengths]))

    print(f"saving visualizations to [{out_path}]...")
    skeleton = paramUtil.kit_kinematic_chain if args.dataset == 'kit' else paramUtil.t2m_kinematic_chain

    # Recover XYZ *positions* from HumanML3D vector representation
    if model.data_rep == 'hml_vec':
        input_motions = data.dataset.t2m_dataset.inv_transform(input_motions.cpu().permute(0, 2, 3, 1)).float()
        input_motions = recover_from_ric(input_motions, n_joints)
        input_motions = input_motions.view(-1, *input_motions.shape[2:]).permute(0, 2, 3, 1).cpu().numpy()


    for sample_i in range(args.num_samples):
        rep_files = []
        for rep_i in range(args.num_repetitions):
            caption = all_text[rep_i*args.batch_size + sample_i]
            if args.guidance_param == 0:
                caption = 'Edit [{}] unconditioned'.format(args.inpainting_mask)
            else:
                caption = 'Edit [{}]: {}'.format(args.inpainting_mask, caption)
            length = all_lengths[rep_i*args.batch_size + sample_i]
            motion = all_motions[rep_i*args.batch_size + sample_i].transpose(2, 0, 1)[:length]
            guidance = {'mask': all_guidance['mask'][rep_i*args.batch_size + sample_i], 'joint': all_guidance['joint'][rep_i*args.batch_size + sample_i]}
            guidance['joint'] = guidance['joint'].transpose(2, 0, 1)[:length] # seq_len, n_joints, 3
            guidance['mask'] = guidance['mask'].transpose(2, 0, 1)[:length]
            save_file = 'sample{:02d}_rep{:02d}.mp4'.format(sample_i, rep_i)
            animation_save_path = os.path.join(out_path, save_file)
            rep_files.append(animation_save_path)
            print(f'[({sample_i}) "{caption}" | Rep #{rep_i} | -> {animation_save_path}]')
            plot_3d_motion(animation_save_path, skeleton, motion, title=caption,
                           dataset=args.dataset, fps=fps, vis_mode=args.inpainting_mask,
                           gt_frames=gt_frames_per_sample.get(sample_i, []), joints2=guidance['joint'], painting_features=args.inpainting_mask.split(','), guidance=guidance)
            # Credit for visualization: https://github.com/EricGuo5513/text-to-motion

        if args.num_repetitions > 1:
            all_rep_save_file = os.path.join(out_path, 'sample{:02d}.mp4'.format(sample_i))
            ffmpeg_rep_files = [f' -i {f} ' for f in rep_files]
            hstack_args = f' -filter_complex hstack=inputs={args.num_repetitions + (1 if args.show_input else 0)} '
            ffmpeg_rep_cmd = f'ffmpeg -y -loglevel warning ' + ''.join(ffmpeg_rep_files) + f'{hstack_args} {all_rep_save_file}'
            os.system(ffmpeg_rep_cmd)
            print(f'[({sample_i}) "{caption}" | all repetitions | -> {all_rep_save_file}]')

    abs_path = os.path.abspath(out_path)
    print(f'[Done] Results are at [{abs_path}]')


if __name__ == "__main__":
    main()