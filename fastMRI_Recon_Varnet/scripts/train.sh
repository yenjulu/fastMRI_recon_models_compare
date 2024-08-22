#!/bin/bash
#SBATCH --gres=gpu:v100:1
#SBATCH --partition=v100
#SBATCH --time=5:50:00

GPU_NUM=0
TRAIN_CONFIG_YAML="configs/fastmri_varnet.yaml"

CUDA_VISIBLE_DEVICES=$GPU_NUM python train.py \
    --config=$TRAIN_CONFIG_YAML \
    --write_image=5 \