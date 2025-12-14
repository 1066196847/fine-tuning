import argparse
from loguru import logger
import os
from os.path import join
import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
import bitsandbytes as bnb
from torch import nn
import deepspeed
# deepspeed.ops.op_builder.CPUAdamBuilder().load()‌ 是DeepSpeed库中的一个函数，
# 用于加载CPUAdam优化器。这个函数通常在初始化DeepSpeed时调用，以确保使用CPU版本的Adam优化器
deepspeed.ops.op_builder.CPUAdamBuilder().load()
from component.collator import PretrainCollator, SFTDataCollator
from component.argument import CustomizedArguments
from component.template import template_dict
from component.dataset import (
    UnifiedSFTDataset,
    ChatGLM2SFTDataset,
    ChatGLM3SFTDataset,
    UnifiedDPODataset,
    UnifiedSFTToolsDataset
)
from transformers import (
    set_seed,
    HfArgumentParser,
    TrainingArguments,
    AutoTokenizer,
    AutoModelForCausalLM,
    AutoConfig,
    BitsAndBytesConfig,
    Trainer,
    AddedToken
)
import importlib
if importlib.util.find_spec('unsloth') is not None:
    from unsloth import FastLanguageModel
from datasets import load_dataset, concatenate_datasets
import datasets
from itertools import chain
from tqdm import tqdm
import json
from trl import DPOTrainer, get_kbit_device_map
import torch.nn as nnF

os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['PATH'] = os.environ["PATH"] + ":/opt/conda/bin/ninja"


def setup_everything():
    '''参数解析，通过解析train_args_file 得到参数'''
    parser = argparse.ArgumentParser()
    # parser.add_argument("--train_args_file", type=str, default='train_args/pretrain/full/bloom-1b1-pretrain-full.json', help="")
    parser.add_argument("--train_args_file", type=str, default='train_args/sft/lora/qwen1.5-7b-sft-lora.json', help="")
    parser.add_argument("--local_rank", type=int, help="")
    args = parser.parse_args()
    train_args_file = args.train_args_file
    # 读取训练的参数配置，把两种参数合并在一起
    # CustomizedArguments：这通常是一个用户自定义的参数类，用于包含除了标准训练参数之外，用户想要添加的任何特定参数
    # TrainingArguments：这是 Hugging Face Transformers 库中预定义的一个类，包含了一系列用于模型训练的标准参数，如学习率、训练轮数（epochs）、批大小（batch size）、输出目录
    parser = HfArgumentParser((CustomizedArguments, TrainingArguments))

    # 解析得到自定义参数，以及自带参数。
    # args就是CustomizedArguments里面的参数；
    # training_args就是TrainingArguments里面的参赛，包括训练命令中指定的train_args_file中的参赛
    # 对于train_args_file中的参赛，比如output_dir不在CustomizedArguments中，那么就自己进到training_args
    # 比如model_name_or_path在CustomizedArguments中，那么就在CustomizedArguments中
    args, training_args = parser.parse_json_file(json_file=train_args_file)
    # 创建输出目录
    if not os.path.exists(training_args.output_dir):
        os.makedirs(training_args.output_dir)
    logger.add(join(training_args.output_dir, 'train.log'))
    logger.info("args:{}".format(args))
    logger.info("train_args:{}".format(training_args))
    # 加载训练配置文件
    with open(train_args_file, "r") as f:
        train_args = json.load(f)
    # 保存训练参数到输出目录
    with open(join(training_args.output_dir, 'train_args.json'), "w") as f:
        json.dump(train_args, f, indent=4)
    # 设置随机种子
    set_seed(training_args.seed)

    # check some setting
    assert args.task_type in ['pretrain', 'sft', 'dpo'], "task_type should be in ['pretrain', 'sft', 'dpo']"
    assert args.train_mode in ['full', 'lora', 'qlora'], "task_type should be in ['full', 'lora', 'qlora']"
    assert sum([training_args.fp16, training_args.bf16]) == 1, "only one of fp16 and bf16 can be True"
    # assert not (args.task_type == 'dpo' and args.use_unsloth), 'We have not tested Unsloth during DPO yet. Please set use_unsloth=False when task_type=dpo'

    return args, training_args
#

def find_all_linear_names(model, train_mode):
    """
    找出所有全连接层，为所有全连接添加adapter
    """
    assert train_mode in ['lora', 'qlora']
    cls = bnb.nn.Linear4bit if train_mode == 'qlora' else nn.Linear
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if 'lm_head' in lora_module_names:  # needed for 16-bit
        lora_module_names.remove('lm_head')
    lora_module_names = list(lora_module_names)
    logger.info(f'LoRA target module names: {lora_module_names}')
    return lora_module_names


def load_pretrain_dataset(training_args, args, tokenizer):
    """
    多线程预处理预训练数据
    """
    def tokenize_function(examples):
        output = tokenizer(examples["text"])
        output = {'input_ids': output.input_ids}
        return output

    def group_texts(examples):
        # Concatenate all texts.
        concatenated_examples = {k: list(chain(*examples[k])) for k in examples.keys()}
        total_length = len(concatenated_examples[list(examples.keys())[0]])
        # We drop the small remainder, we could add padding if the model supported it instead of this drop, you can
        # customize this part to your needs.
        if total_length >= max_seq_length:
            total_length = (total_length // max_seq_length) * max_seq_length
        # Split by chunks of max_len.
        result = {
            k: [t[i: i + max_seq_length] for i in range(0, total_length, max_seq_length)]
            for k, t in concatenated_examples.items()
        }
        return result

    data_path = args.train_file # ./data/summary/sum_train_2048.jsonl
    max_seq_length = args.max_seq_length # 2048
    # 创建缓存路径
    cache_dir = join(data_path, 'cache') # ./data/summary/cache
    os.makedirs(cache_dir, exist_ok=True)
    logger.info('Pretraining data path: {}'.format(data_path))

    # 扫描所有jsonl文件
    logger.info('Scanning all the training file...')
    files = []
    # 如果data_path的取值是：./data/summary/
    # 那么3个值是：./data/summary/   []   ['sum_train_2048.jsonl', 'summary_test.jsonl']
    for root, dir_names, file_names in os.walk(data_path):
        for file_name in file_names:
            file = join(root, file_name)
            if file_name.endswith('.jsonl') and "train" in file_names:
                files.append(file)
    files.append(data_path)
    logger.info(f'Total num of training file: {len(files)}, files = {files}')

    # 预处理所有文本，将其id化，并且进行packing操作
    # 它的设计初衷是为了确保某些操作只在主进程（通常是负责协调和管理其他进程的进程）上执行。这在多进程训练环境中特别有用，因为可以避免由于多个进程同时执行相同操作而导致的资源冲突或数据不一致问题
    with training_args.main_process_first(desc="dataset map tokenization and grouping"):
        pretrain_dataset = []  # 汇总所有dataset
        for idx, file in enumerate(tqdm(files)):
            logger.info(f'Loading file: {file}')
            file_name = os.path.basename(file) # sum_train_2048.jsonl
            file_name = file_name.replace('.jsonl', '') # sum_train_2048
            cache_path = os.path.join(cache_dir, file_name) # ./data/summary/cache/sum_train_2048
            os.makedirs(cache_path, exist_ok=True)
            logger.info(f'cache_path: {cache_path}')

            try:
                processed_dataset = datasets.load_from_disk(cache_path, keep_in_memory=False)
                logger.info(f'Finished loading datasets-{file_name} from cache')
            except Exception:
                tmp_cache_path = join(cache_path, 'tmp')    # 临时缓存目录，会被自动删除
                logger.info(f'There is no cache of file {file_name}, start preprocessing...')
                # 您可以使用raw_dataset这个对象来遍历数据、应用转换（如分词、编码等）、划分训练集和测试集
                raw_dataset = load_dataset("json", data_files=file, cache_dir=tmp_cache_path, keep_in_memory=False)
                tokenized_dataset = raw_dataset.map(
                    tokenize_function,
                    batched=True,
                    num_proc=args.tokenize_num_workers,
                    remove_columns="text",
                    load_from_cache_file=True,
                    keep_in_memory=False,
                    cache_file_names={k: os.path.join(tmp_cache_path, 'tokenized.arrow') for k in raw_dataset},
                    desc="Running tokenizer on dataset",
                )
                grouped_datasets = tokenized_dataset.map(
                    group_texts,
                    batched=True,
                    num_proc=args.tokenize_num_workers,
                    load_from_cache_file=True,
                    keep_in_memory=False,
                    cache_file_names={k: os.path.join(tmp_cache_path, 'grouped.arrow') for k in tokenized_dataset},
                    desc=f"Grouping texts in chunks of {max_seq_length}",
                )
                processed_dataset = grouped_datasets
                processed_dataset.save_to_disk(cache_path)
                # 删除临时目录
                # shutil.rmtree(tmp_cache_path)

            logger.info(f"Training number of {file_name}: {len(processed_dataset['train'])}")
            if idx == 0:
                pretrain_dataset = processed_dataset['train']
            else:
                assert pretrain_dataset.features.type == processed_dataset["train"].features.type
                pretrain_dataset = concatenate_datasets([pretrain_dataset, processed_dataset["train"]])
    logger.info(f"Total training number: {len(pretrain_dataset)}")
    return pretrain_dataset


def load_tokenizer(args):
    '''读取tokenizer分词器'''
    config = AutoConfig.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    # 加载tokenzier
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        #这个参数允许从远程源（如 Hugging Face Models Hub）加载和执行可能包含自定义代码（如特殊分词逻辑）的模型。这在某些情况下是必要的，因为一些模型可能包含特定的预处理或后处理步骤
        trust_remote_code=True,
        # 这个参数决定了是否应该使用基于 Rust 的 tokenizers 库（通常称为“fast”分词器）来加速分词过程
        # llama不支持fast
        use_fast=False if config.model_type == 'llama' or config.model_type == 'internlm2' else True
    )

    # 部分模型的base与chat版本的tokenizer存在差异
    if 'internlm2' in args.model_name_or_path.lower():
        tokenizer._added_tokens_encoder.update({'<|im_start|>': 92543})
        tokenizer._added_tokens_encoder.update({'<|im_end|>': 92542})
        tokenizer._added_tokens_decoder.update({92543: AddedToken('<|im_start|>')})
        tokenizer._added_tokens_decoder.update({92542: AddedToken('<|im_end|>')})
        tokenizer.add_special_tokens({'additional_special_tokens': ['<|im_start|>', '<|im_end|>']})
    elif 'orion' in args.model_name_or_path.lower():
        tokenizer.add_special_tokens({'bos_token': '<s>', 'eos_token': '</s>'})
    elif 'gemma' in args.model_name_or_path.lower():
        tokenizer.add_special_tokens({'additional_special_tokens': ['<start_of_turn>', '<end_of_turn>']})

    # tokenizer.__class__.__name__：Qwen2TokenizerFast
    # 'Qwen2TokenizerFast' object has no attribute 'eod_id'
    if tokenizer.__class__.__name__ == 'QWenTokenizer':
        logger.info(f'tokenizer.__class__.__name__ ==  QWenTokenizer {tokenizer.eod_id}')
        # 这些属性通常在使用分词器处理输入数据之前被查询，以便在创建模型输入时正确地设置这些特殊标记的ID。
        # 例如，在将文本转换为模型可以理解的ID序列时，你可能会在序列的开始添加 bos_token_id，
        # 在序列的结束添加 eos_token_id，并确保所有序列都具有相同的长度（通过添加 pad_token_id 来填充较短的序列）
        tokenizer.pad_token_id = tokenizer.eod_id # 填充标记的ID
        tokenizer.bos_token_id = tokenizer.eod_id # 开始序列标记的ID
        tokenizer.eos_token_id = tokenizer.eod_id # 结束序列标记的ID
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    assert tokenizer.pad_token_id is not None, "pad_token_id should not be None" # 151643
    assert tokenizer.eos_token_id is not None, "eos_token_id should not be None" # 151643
    logger.info(f'vocab_size of tokenizer: {tokenizer.vocab_size}') # 151643
    return tokenizer


def load_unsloth_model(args, training_args):
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model_name_or_path,
        max_seq_length=args.max_seq_length,
        dtype=None,
        trust_remote_code=True,
        load_in_4bit=True if args.train_mode == 'qlora' else False,
    )
    if args.train_mode in ['lora', 'qlora']:
        logger.info('Initializing PEFT Model...')
        target_modules = find_all_linear_names(model, args.train_mode)
        model = FastLanguageModel.get_peft_model(
            model,
            r=args.lora_rank,
            target_modules=target_modules,
            lora_alpha=args.lora_alpha,
            lora_dropout=args.lora_dropout,
            bias="none",
            use_gradient_checkpointing=True,
            random_state=training_args.seed,
            max_seq_length=args.max_seq_length,
        )
        logger.info(f'target_modules: {target_modules}')
    return {
        'model': model,
        'ref_model': None,
        'peft_config': None
    }


def load_model(args, training_args):
    """
    加载模型
    """
    # fp16=false，bf16=true
    assert training_args.bf16 or training_args.fp16, 'bf16 or fp16 should be True'
    logger.info(f'Loading model from base model: {args.model_name_or_path}')
    logger.info(f'Train model with {args.train_mode}') # full

    # init model kwargs
    # todo add flash attention
    # attn_implementation = None
    torch_dtype = torch.float16 if training_args.fp16 else torch.bfloat16
    if args.train_mode == 'qlora':
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16 if training_args.fp16 else torch.bfloat16,
            bnb_4bit_use_double_quant=True,
            bnb_4bit_quant_type="nf4",
            llm_int8_threshold=6.0,
            llm_int8_has_fp16_weight=False,
        )
    else:
        quantization_config = None
    model_kwargs = dict(
        trust_remote_code=True, # 加载远程代码
        attn_implementation="flash_attention_2", # 注意力机制，flash_attention 可加速，目前支持Nvidia GPU显卡（如H100、A100、RTX X090、T4) ，如果不支持的话，这一行注释掉即可
        torch_dtype=torch_dtype, # 模型精度
        # gradient_checkpointing取值是false
        # 如果显存捉襟见肘，可以开启。以时间换空间，模型不缓存激活状态，会进行两次forward计算，以节省显存
        use_cache=False if training_args.gradient_checkpointing else True, # 是否使用缓存，加速推理
        device_map=get_kbit_device_map() if quantization_config is not None else None, # 模型配置
        quantization_config=quantization_config, # 量化配置
    )
    model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, **model_kwargs) # 模型加载

    # moe模型，需要考虑负载均衡的loss
    # qwen模型不包含'output_router_logits'
    if 'output_router_logits' in model.config.to_dict():
        logger.info('set output_router_logits as True')
        model.config.output_router_logits = True
    # QLoRA: casts all the non int8 modules to full precision (fp32) for stability
    # 将所有的非int8类型的模块（可能是指层、操作或参数）转换为全精度（fp32，即32位浮点数）格式，以提高数值计算的稳定性
    if args.train_mode == 'qlora' and args.task_type in ['pretrain', 'sft']:
        model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=training_args.gradient_checkpointing)
    # LoRA: Enables the gradients for the input embeddings
    if args.train_mode == 'lora' and args.task_type in ['pretrain', 'sft']:
        # For backward compatibility 为了向后兼容性
        # enable_input_require_grads的功能是启用输入张量的梯度计算要求，qwen没有启动
        # 在默认情况下，许多深度学习框架（如PyTorch）中的输入张量（即模型的输入数据）的梯度是不会被计算的
        # 这是因为，在大多数训练任务中，我们只需要计算模型参数（如权重和偏置）的梯度，以便通过反向传播算法更新这些参数
        # 这一功能在多种场景下可能非常有用。例如，在元学习、对抗性训练或某些类型的模型压缩技术中，可能需要计算输入张量的梯度
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
        else:
            # 这里本来的目的应该是对输入层保留计算的梯度，但是文心一言有一些不一样的解释，可以用下面的prompt询问下
            # =================== prompt开始
            # 已知加载的是qwen1.5B的模型，args.model_name_or_path是qwen模型的路径，请你解释下下面的代码
            # model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, **model_kwargs)
            #             def make_inputs_require_grad(module, input, output):
            #                 output.requires_grad_(True)
            #             model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)
            # =================== prompt结束
            def make_inputs_require_grad(module, input, output):
                output.requires_grad_(True)
            model.get_input_embeddings().register_forward_hook(make_inputs_require_grad)

    # init peft_config
    if args.train_mode == 'full':
        peft_config = None
    else:
        # 找到所有需要插入adapter的全连接层
        target_modules = find_all_linear_names(model, args.train_mode)
        peft_config = LoraConfig(
            r=args.lora_rank,
            lora_alpha=args.lora_alpha,
            target_modules=target_modules,
            lora_dropout=args.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
        )

    # init peft model
    if args.train_mode in ['lora', 'qlora'] and args.task_type in ['pretrain', 'sft']:
        model = get_peft_model(model, peft_config)
        logger.info(f'memory footprint of model: {model.get_memory_footprint() / (1024 * 1024 * 1024)} GB')
        model.print_trainable_parameters()

    # init ref_model
    if args.task_type == 'dpo':
        ref_model = AutoModelForCausalLM.from_pretrained(args.model_name_or_path, **model_kwargs) if args.train_mode == 'full' else None
    # pretrain和sft，不需要ref_model
    else:
        ref_model = None

    # 计算模型参数量
    total = sum(p.numel() for p in model.parameters())
    logger.info("Total model params: %.2fM" % (total / 1e6))

    return {
        'model': model,
        'ref_model': ref_model,
        'peft_config': peft_config
    }


def load_sft_dataset(args, tokenizer):
    # args.template_name=qwen
    if args.template_name not in template_dict.keys():
        raise Exception(f"template_name doesn't exist, all template_name: {template_dict.keys()}")
    template = template_dict[args.template_name]
    if 'chatglm2' in args.model_name_or_path.lower():
        logger.info('Loading data with ChatGLM2SFTDataset')
        train_dataset = ChatGLM2SFTDataset(args.train_file, tokenizer, args.max_seq_length, template)
    elif 'chatglm3' in args.model_name_or_path.lower():
        logger.info('Loading data with ChatGLM3SFTDataset')
        train_dataset = ChatGLM3SFTDataset(args.train_file, tokenizer, args.max_seq_length, template)
    else:
        logger.info('Loading data with UnifiedSFTDataset')
        # args.train_file: ./data/summary/sum_train_2048.jsonl
        # max_seq_length:2048
        # template：qwen对应的模版
        train_dataset = UnifiedSFTDataset(args.train_file, tokenizer, args.max_seq_length, template)

    return train_dataset


def load_dpo_dataset(args, tokenizer):
    if args.template_name not in template_dict.keys():
        raise Exception(f"template_name doesn't exist, all template_name: {template_dict.keys()}")
    template = template_dict[args.template_name]
    train_dataset = UnifiedDPODataset(args.train_file, tokenizer, args.max_seq_length, args.max_prompt_length, template)
    return train_dataset


def init_components(args, training_args):
    """
    初始化各个组件
    """
    # 设置False的功能1：设置False可以减少DDP在跟踪和处理这些未使用参数时的开销，从而提高训练效率
    # 功能2：确信所有参数都会被使用，或者不关系未使用
    training_args.ddp_find_unused_parameters = False
    logger.info('Initializing components...')

    # 加载tokenizer
    tokenizer = load_tokenizer(args)
    # 加载model。args.use_unsloth取值是false
    if args.use_unsloth:
        components = load_unsloth_model(args, training_args)
    else:
        components = load_model(args, training_args)
    model = components['model']
    ref_model = components['ref_model']
    peft_config = components['peft_config']

    # 初始化dataset和collator
    if args.task_type == 'pretrain':
        logger.info('Train model with pretrain task')
        train_dataset = load_pretrain_dataset(training_args, args, tokenizer)
        data_collator = PretrainCollator(tokenizer, args.max_seq_length)
    elif args.task_type == 'sft':
        logger.info('Train model with sft task')
        train_dataset = load_sft_dataset(args, tokenizer)
        data_collator = SFTDataCollator(tokenizer, args.max_seq_length)
    else:
        logger.info('Train model with dpo task')
        train_dataset = load_dpo_dataset(args, tokenizer)
        data_collator = None

    # dpo
    if args.task_type == 'dpo':
        trainer = DPOTrainer(
            model,
            ref_model,
            args=training_args,
            beta=args.beta,
            train_dataset=train_dataset,
            data_collator=data_collator,
            tokenizer=tokenizer,
            peft_config=peft_config
        )
    # pretrain or sft
    else:
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            tokenizer=tokenizer,
            data_collator=data_collator,
        )
    return trainer


def main():
    # 进行一些配置和检查
    args, training_args = setup_everything()
    # 加载各种组件
    trainer = init_components(args, training_args)
    # 开始训练
    logger.info("*** starting training ***")
    train_result = trainer.train()
    # 保存最好的checkpoint
    final_save_path = join(training_args.output_dir)
    trainer.save_model(final_save_path)  # Saves the tokenizer too
    # 保存训练指标
    metrics = train_result.metrics
    #   ***** train metrics *****
    #   epoch                    =     0.9981
    #   total_flos               =  2835438GF
    #   train_loss               =     1.5457
    #   train_runtime            = 0:05:57.28
    #   train_samples_per_second =      2.891
    #   train_steps_per_second   =      0.722
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics) # 看源码能看到最终保存的文件名是：train_results.json
    trainer.save_state() # trainer_state.json


if __name__ == "__main__":
    main()
