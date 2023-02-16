#!/usr/bin/env python3
# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Finetuning a 🤗 Transformers model for sequence classification on GLUE."""
import argparse
import logging
import math
import os
import random
from datetime import datetime
from pathlib import Path

import datasets
from datasets import load_from_disk, load_metric
from tokenizers import Tokenizer
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from accelerate import Accelerator, DistributedDataParallelKwargs
from huggingface_hub import Repository
from transformers import (
    AdamW,
    AutoTokenizer,
    get_scheduler,
    set_seed,
    DataCollatorWithPadding,
    AutoModelForSequenceClassification,
    AutoConfig
)
from transformers.file_utils import get_full_repo_name
from transformers.utils.versions import require_version

from utils import custom_tokenize, load_args, save_args, path_adder, preprocess_function, MODEL_MAPPING, select_base
from model_utils import freeze_base, copy_proj_layers, pretrained_masked_model_selector, pretrained_model_selector, pretrained_sequence_model_selector
from data_collator import CustomDataCollator
from models import HierarchicalClassificationModel
from longformer import get_attention_injected_model


logger = logging.getLogger(__name__)

require_version("datasets>=1.8.0", "To fix: pip install -r examples/pytorch/text-classification/requirements.txt")


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune the hierarchical model on a text classification task")
    parser.add_argument(
        "--train_file", type=str, default=None, help="A csv or a json file containing the training data."
    )
    parser.add_argument(
        "--validation_file", type=str, default=None, help="A csv or a json file containing the validation data."
    )
    parser.add_argument(
        "--validation_split_percentage",
        default=0.10,
        help="The percentage of the train set used as validation set in case there's no validation split",
    )
    parser.add_argument(
        # Modified
        "--max_seq_length",
        type=int,
        default=None,
        help=(
            "The maximum total input sequence length after tokenization. Sequences longer than this will be truncated,"
            " sequences shorter will be padded if `--pad_to_max_lengh` is passed."
        ),
    )
    parser.add_argument(
        "--pad_to_max_length",
        action="store_true",
        help="If passed, pad all samples to `max_length`. Otherwise, dynamic padding is used.",
    )
    parser.add_argument(
        # Modified
        "--pretrained_dir",
        type=str,
        help="Path to the output directory of pretraining step.",
        required=True,
    )
    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the 🤗 Tokenizers library).",
    )
    parser.add_argument(
        "--overwrite_cache", type=bool, default=False, help="Overwrite the cached training and evaluation sets"
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=3, help="Total number of training epochs to perform.")
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        # Modified for saving arguments
        type=str,
        default="linear",
        help="The scheduler type to use.",
        choices=["linear", "cosine", "cosine_with_restarts", "polynomial", "constant", "constant_with_warmup"],
    )
    parser.add_argument(
        "--num_warmup_steps", type=int, default=0, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument(
        "--hub_model_id", type=str, help="The name of the repository to keep in sync with the local `output_dir`."
    )
    parser.add_argument("--hub_token", type=str, help="The token to use to push to the Model Hub.")
    # Modified:
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--max_document_length",
        type=int,
        default=None,
        required=True,
        help="The maximum number of sentences each document can have. Documents are either truncated or"
             "padded if their length is different.",
    )
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="Either the lower level encoder is frozen or not."
    )
    parser.add_argument(
        "--unfreeze",
        action="store_true",
        help="If True, unfreezes the whole model."
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="If True, freeze the whole model other than the classifier."
    )
    parser.add_argument(
        "--dropout",
        type=float,
        default=None,
        help="The dropout value of upper level encoder.",
    )
    parser.add_argument(
        "--lower_dropout",
        type=float,
        default=0.1,
        help="The dropout value of lower level encoder.",
    )
    parser.add_argument(
        "--pretrained_epoch",
        type=int,
        default=2,
        help="Checkpoint from pretraining to use.",
    )
    parser.add_argument(
        "--logging_steps",
        type=int,
        default=500,
        help="Frequency of logging mini-batch loss .",
    )
    parser.add_argument(
        "--custom_model",
        type=str,
        help="If a custom model is to be used, the model type has to be specified.",
        default=None,
        choices=["hierarchical", "sliding_window", "longformer"]
    )
    parser.add_argument(
        "--custom_from_scratch",
        action="store_true",
        help="If True, then the custom model is not initilazed from the previously pretrained model."
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=None,
        help="The length of the stride, when sliding window approach is used.",
    )
    parser.add_argument(
        "--lower_pooling",
        type=str,
        default=None,
        help="The pooling to be used for the lower level encoder.",
    )
    parser.add_argument(
        "--upper_pooling",
        type=str,
        default=None,
        help="The pooling to be used for the upper level encoder.",
    )
    parser.add_argument(
        "--max_patience",
        type=int,
        default=7,
        help="The number of epochs to wait before early stopping.",
    )
    args = parser.parse_args()

    # Sanity checks
    if args.train_file is None:
        raise ValueError("Need training file.")

    if args.push_to_hub:
        assert args.output_dir is not None, "Need an `output_dir` to create a repo when `--push_to_hub` is passed."

    return args


def main():
    # Modified: classification arguments
    args = parse_args()

    # Argments from pretraining
    if args.custom_model == "hierarchical":
        pretrained_args = load_args(os.path.join(args.pretrained_dir, "args.json"))
        args.use_sliding_window_tokenization = getattr(pretrained_args , "use_sliding_window_tokenization", False)
    elif args.custom_model == "sliding_window":
        args.use_sliding_window_tokenization = True

    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    ddp_kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)
    accelerator = Accelerator(kwargs_handlers=[ddp_kwargs])

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.push_to_hub:
            if args.hub_model_id is None:
                repo_name = get_full_repo_name(Path(args.output_dir).name, token=args.hub_token)
            else:
                repo_name = args.hub_model_id
            repo = Repository(args.output_dir, clone_from=repo_name)
        elif args.output_dir is not None:
            # Modified: output_dir is concatanated with datetime and command line arguments are also saved
            # TODO: refactor
            if args.custom_model == "hierarchical":
                inter_path = path_adder(pretrained_args, finetuning=True, custom_model=args.custom_model, c_args=args)
            elif args.custom_model == "longformer":
                inter_path = path_adder(args, finetuning=True, custom_model=args.custom_model, c_args=args)
            else:
                inter_path = path_adder(args, finetuning=True)
            inter_path += datetime.now().strftime("%Y_%m_%d-%H_%M_%S")
            args.output_dir = os.path.join(args.output_dir, inter_path)
            os.makedirs(args.output_dir, exist_ok=True)
            save_args(args)
            if args.custom_model == "hierarchical":
                save_args(pretrained_args, args_path=args.output_dir, pretrained=True)

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
        # Modified
        handlers=[
            logging.FileHandler(os.path.join(args.output_dir, "loginfo.log")),
            logging.StreamHandler()
        ]
    )
    logger.info(accelerator.state)

    # Setup logging, we only want one process per machine to log things on the screen.
    # accelerator.is_local_main_process is only True for one process per machine.
    logger.setLevel(logging.INFO if accelerator.is_local_main_process else logging.ERROR)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()

    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    accelerator.wait_for_everyone()

    # Modified:
    raw_datasets = load_from_disk(args.train_file)

    # Labels
    # Modified
    label_list = raw_datasets["train"].unique("labels")
    label_list.sort()  # Let's sort it for determinism
    num_labels = len(label_list)

    # Load pretrained model and tokenizer
    # TODO: change to classification arguments
    # TODO: additional condition for model type
    if args.custom_model == "longformer":
        tokenizer = AutoTokenizer.from_pretrained(
        args.pretrained_dir,
        max_length=args.max_seq_length,
        padding="max_length",
        truncation=True,
        )
    else:
        tokenizer = AutoTokenizer.from_pretrained(args.pretrained_dir,
                                                  use_fast=True)

    if args.custom_model in ("hierarchical", "sliding_window"):
        model = HierarchicalClassificationModel(c_args=args,
                                                args=None if args.custom_model == "sliding_window" else pretrained_args,
                                                tokenizer=tokenizer,
                                                num_labels=num_labels)
    elif args.custom_model == "longformer":
        psm = pretrained_sequence_model_selector(select_base(args.pretrained_dir))
        model = get_attention_injected_model(psm)
        model = model.from_pretrained(
            args.pretrained_dir,  # /checkpoint-14500
            max_length=args.max_seq_length,
            num_labels=num_labels
        )
    else:
        config = AutoConfig.from_pretrained(args.pretrained_dir, num_labels=num_labels)
        model = AutoModelForSequenceClassification.from_pretrained(
            args.pretrained_dir,
            config=config,
        )
        if args.frozen:
            freeze_base(model)

    if args.custom_model in ("hierarchical", "sliding_window"):
        with accelerator.main_process_first():
            # Modified
            ARTICLE_NUMBERS = 1
            raw_datasets = raw_datasets.rename_column("text", "article_1")
            processed_datasets = raw_datasets.map(
                custom_tokenize,
                fn_kwargs={"tokenizer": tokenizer, "args": args, "article_numbers": ARTICLE_NUMBERS},
                num_proc=args.preprocessing_num_workers,
                load_from_cache_file=False,
                desc="Running tokenizer on dataset",
            )
    else:
        with accelerator.main_process_first():
            processed_datasets = raw_datasets.map(
                preprocess_function,
                fn_kwargs={"tokenizer": tokenizer, "max_seq_length": args.max_seq_length},
                batched=True,
                num_proc=args.preprocessing_num_workers,
                remove_columns=raw_datasets["train"].column_names,
                load_from_cache_file=False,
                desc="Running tokenizer on dataset",
            )

    # Modified
    train_dataset = processed_datasets["train"]
    eval_dataset = processed_datasets["validation"]

    # Log a few random samples from the training set:
    for index in random.sample(range(len(train_dataset)), 3):
        logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")

    if args.custom_model in ("hierarchical", "sliding_window"):
        # Modified
        ARTICLE_NUMBERS = 1
        data_collator = CustomDataCollator(tokenizer=tokenizer,
                                           max_sentence_len=pretrained_args.max_seq_length if args.max_seq_length is None else args.max_seq_length,
                                           max_document_len=pretrained_args.max_document_length if args.max_document_length is None else args.max_document_length,
                                           article_numbers=ARTICLE_NUMBERS,
                                           consider_dcls=True if args.custom_model == "hierarchical" else False)
    elif args.custom_model == "longformer":
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=512)
    else:
        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=(8 if accelerator.use_fp16 else None))

    train_dataloader = DataLoader(
        train_dataset, shuffle=True, collate_fn=data_collator, batch_size=args.per_device_train_batch_size
    )
    eval_dataloader = DataLoader(eval_dataset, collate_fn=data_collator, batch_size=args.per_device_eval_batch_size)

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]
    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate)

    # Prepare everything with our `accelerator`.
    model, optimizer, train_dataloader, eval_dataloader = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader
    )

    # Note -> the training dataloader needs to be prepared before we grab his length below (cause its length will be
    # shorter in multiprocess)

    # Scheduler and math around the number of training steps.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
    else:
        args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    lr_scheduler = get_scheduler(
        name=args.lr_scheduler_type,
        optimizer=optimizer,
        num_warmup_steps=args.num_warmup_steps,
        num_training_steps=args.max_train_steps,
    )

    metric = load_metric("accuracy")
    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")
    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0

    # Modified for checkpoint saving:
    best_score = float("inf")
    patience = 0

    for epoch in range(args.num_train_epochs):
        model.train()
        running_loss = 0.0
        validation_loss = 0.0
        for step, batch in enumerate(train_dataloader):
            # Modified for Hierarchical Classification Model
            outputs = model(**batch)
            loss = outputs.loss
            loss = loss / args.gradient_accumulation_steps
            accelerator.backward(loss)
            if step % args.gradient_accumulation_steps == 0 or step == len(train_dataloader) - 1:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
                progress_bar.update(1)
                completed_steps += 1
            # TODO: change
                # running_loss += loss.item()
                running_loss += loss.item() * batch["labels"].shape[0]
            # if step % args.logging_steps == args.logging_steps - 1:
            #     logger.info(f"epoch: {epoch}, step {step+1}:, loss: {running_loss/args.logging_steps}")
            #     running_loss = 0.0
            if completed_steps >= args.max_train_steps:
                break

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # Modified for Hierarchical Classification Model
            with torch.no_grad():
                outputs = model(**batch)
            validation_loss += outputs.loss.item() * batch["labels"].shape[0]
            predictions = outputs.logits.argmax(dim=-1)
            metric.add_batch(
                predictions=accelerator.gather(predictions),
                references=accelerator.gather(batch["labels"]),
            )

        eval_metric = metric.compute()
        # logger.info(f"epoch {epoch}: {eval_metric}")
        train_loss = running_loss / len(train_dataset)
        validation_loss = validation_loss / len(eval_dataset)
        logger.info(
            f"epoch {epoch}| accuracy: {eval_metric}, train loss: {train_loss:.4f}"
            f", validation loss: {validation_loss:.4f}"
        )
        
        # TODO: save checkpoints
        if validation_loss < best_score:
            patience = 0
            best_score = validation_loss
            accelerator.wait_for_everyone()
            unwrapped_model = accelerator.unwrap_model(model)
            # Modified
            # TODO: change for other models
            if args.custom_model in ("hierarchical", "sliding_window"):
                accelerator.save(obj=unwrapped_model.state_dict(),
                                 f=args.output_dir + "/model.pth")
            else:
                unwrapped_model.save_pretrained(args.output_dir, save_function=accelerator.save)
            logger.info(f"model after epoch {epoch} is saved")
            if accelerator.is_main_process:
                tokenizer.save_pretrained(args.output_dir)
        else:
            patience += 1
            if patience == args.max_patience:
                logger.info(f"Traning stopped after the epoch {epoch}, due to patience parameter.")
                break

        # # TODO: save checkpoints
        # if eval_metric['accuracy'] > best_score:
        #     best_score = eval_metric['accuracy']
        #     accelerator.wait_for_everyone()
        #     unwrapped_model = accelerator.unwrap_model(model)
        #     # Modified
        #     # TODO: change for other models
        #     if args.custom_model in ("hierarchical", "sliding_window"):
        #         accelerator.save(obj=unwrapped_model.state_dict(),
        #                          f=args.output_dir + "/model.pth")
        #     else:
        #         unwrapped_model.save_pretrained(args.output_dir, save_function=accelerator.save)
        #     logger.info(f"model after epoch {epoch} is saved")
        #     if accelerator.is_main_process:
        #         tokenizer.save_pretrained(args.output_dir)


if __name__ == "__main__":
    main()
