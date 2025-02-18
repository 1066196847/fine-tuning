export CUDA_VISIBLE_DEVICES=0,1

torchrun --nnodes 1 --nproc_per_node 2 --master_port 25641 \
/root/fine-tuning/train.py \
--train_args_file /root/fine-tuning/train_args/sft/lora/qwen2-1.5b-sft-lora.json
