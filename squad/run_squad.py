# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
""" Finetuning the library models for question-answering on SQuAD (DistilBERT, Bert, XLM, XLNet)."""

import glob
import logging
import os
import random
import timeit

import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from transformers import (
  MODEL_FOR_QUESTION_ANSWERING_MAPPING,
  WEIGHTS_NAME,
  AdamW,
  AutoConfig,
  AutoModelForQuestionAnswering,
  AutoTokenizer,
  get_linear_schedule_with_warmup,
  squad_convert_examples_to_features,
)
from transformers.data.metrics.squad_metrics import (
  compute_predictions_log_probs,
  compute_predictions_logits,
  squad_evaluate,
)
from transformers.data.processors.squad import SquadResult, SquadV1Processor, SquadV2Processor
from arguments import get_parser

try:
  from torch.utils.tensorboard import SummaryWriter
except ImportError:
  from tensorboardX import SummaryWriter


logger = logging.getLogger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_FOR_QUESTION_ANSWERING_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)

ALL_MODELS = sum((tuple(conf.pretrained_config_archive_map.keys()) for conf in MODEL_CONFIG_CLASSES), (),)


def set_seed(args):
  random.seed(args.seed)
  np.random.seed(args.seed)
  torch.manual_seed(args.seed)
  if args.n_gpu > 0:
    torch.cuda.manual_seed_all(args.seed)


def to_list(tensor):
  return tensor.detach().cpu().tolist()

def entropy(tensor):
  return torch.sum(torch.log(tensor) * tensor, axis=-1)


def train(args, train_dataset, model, tokenizer, uncertainty_model=None):
  """ Train the model """
  if args.local_rank in [-1, 0]:
    tb_writer = SummaryWriter()

  args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
  train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
  train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

  if args.max_steps > 0:
    t_total = args.max_steps
    args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
  else:
    t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

  # Prepare optimizer and schedule (linear warmup and decay)
  no_decay = ["bias", "LayerNorm.weight"]
  optimizer_grouped_parameters = [
    {
      "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
      "weight_decay": args.weight_decay,
    },
    {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
  ]
  optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
  scheduler = get_linear_schedule_with_warmup(
    optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total
  )

  if uncertainty_model:
    uncertainty_optimizer = torch.optim.Adam(uncertainty_model.parameters)

  # Check if saved optimizer or scheduler states exist
  if os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt")) and os.path.isfile(
    os.path.join(args.model_name_or_path, "scheduler.pt")
  ):
    # Load in optimizer and scheduler states
    optimizer.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "optimizer.pt")))
    scheduler.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "scheduler.pt")))
  
  if uncertainty_model and os.path.isfile(os.path.join(args.model_name_or_path, "uncertainty_optimizer.pt")):
    uncertainty_model.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "uncertainty_optimizer.pt")))

  if args.fp16:
    try:
      from apex import amp
    except ImportError:
      raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")

    model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)
    if uncertainty_model:
      uncertainty_model, uncertainty_optimizer = amp.initialize(uncertainty_model, uncertainty_optimizer, opt_level=args.fp16_opt_level)

  # multi-gpu training (should be after apex fp16 initialization)
  if args.n_gpu > 1:
    model = torch.nn.DataParallel(model)
    uncertainty_model = torch.nn.DataParallel(uncertainty_model)

  # Distributed training (should be after apex fp16 initialization)
  if args.local_rank != -1:
    model = torch.nn.parallel.DistributedDataParallel(
      model, device_ids=[args.local_rank], output_device=args.local_rank, find_unused_parameters=True
    )

  # Train!
  logger.info("***** Running training *****")
  logger.info("  Num examples = %d", len(train_dataset))
  logger.info("  Num Epochs = %d", args.num_train_epochs)
  logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
  logger.info(
    "  Total train batch size (w. parallel, distributed & accumulation) = %d",
    args.train_batch_size
    * args.gradient_accumulation_steps
    * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
  )
  logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
  logger.info("  Total optimization steps = %d", t_total)

  global_step = 1
  epochs_trained = 0
  steps_trained_in_current_epoch = 0
  # Check if continuing training from a checkpoint
  if os.path.exists(args.model_name_or_path):
    try:
      # set global_step to gobal_step of last saved checkpoint from model path
      checkpoint_suffix = args.model_name_or_path.split("-")[-1].split("/")[0]
      global_step = int(checkpoint_suffix)
      epochs_trained = global_step // (len(train_dataloader) // args.gradient_accumulation_steps)
      steps_trained_in_current_epoch = global_step % (len(train_dataloader) // args.gradient_accumulation_steps)

      logger.info("  Continuing training from checkpoint, will skip to saved global_step")
      logger.info("  Continuing training from epoch %d", epochs_trained)
      logger.info("  Continuing training from global step %d", global_step)
      logger.info("  Will skip the first %d steps in the first epoch", steps_trained_in_current_epoch)
    except ValueError:
      logger.info("  Starting fine-tuning.")

  tr_loss, logging_loss = 0.0, 0.0
  model.zero_grad()
  train_iterator = trange(
    epochs_trained, int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0]
  )
  # Added here for reproductibility
  set_seed(args)

  for _ in train_iterator:
    epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
    for step, batch in enumerate(epoch_iterator):

      # Skip past any already trained steps if resuming training
      if steps_trained_in_current_epoch > 0:
        steps_trained_in_current_epoch -= 1
        continue

      model.train()
      batch = tuple(t.to(args.device) for t in batch)

      inputs = {
        "input_ids": batch[0],
        "attention_mask": batch[1],
        "token_type_ids": batch[2],
        "start_positions": batch[3],
        "end_positions": batch[4],
      }

      if args.model_type in ["xlm", "roberta", "distilbert", "camembert"]:
        del inputs["token_type_ids"]

      if args.model_type in ["xlnet", "xlm"]:
        inputs.update({"cls_index": batch[5], "p_mask": batch[6]})
        if args.version_2_with_negative:
          inputs.update({"is_impossible": batch[7]})
        if hasattr(model, "config") and hasattr(model.config, "lang2id"):
          inputs.update(
            {"langs": (torch.ones(batch[0].shape, dtype=torch.int64) * args.lang_id).to(args.device)}
          )

      outputs = model(**inputs)
      # model outputs are always tuple in transformers (see doc)
      loss = outputs[0]
      if uncertainty_model:
        start_scores = outputs[1]
        end_scores = outputs[2]
        attentions = outputs[max(3, len(outputs)-1)]

        start_scores[1-inputs['token_type_ids']] = -10000
        end_scores[1-inputs['token_type_ids']] = -10000
        start_scores = torch.softmax(start_scores, dim=-1)
        end_scores = torch.softmax(end_scores, dim=-1)
        scores = torch.stack(start_scores, end_scores, dim=0)

        offsets = torch.sum(inputs['attention_mask'], axis=1) - torch.sum(inputs['token_type_ids'], axis=1)
        ends = torch.sum(inputs['attention_mask'], axis=1)
        lengths = (offsets - ends).flatten()
        for idx, (offset, end) in enumerate(zip(offsets, ends)):
          a = [entropy(attention[idx, :, offset:end, :]) / lengths[idx] for attention in attentions]
          import pdb
          pdb.set_trace()
          


        

      if args.n_gpu > 1:
        loss = loss.mean()  # mean() to average on multi-gpu parallel (not distributed) training
      if args.gradient_accumulation_steps > 1:
        loss = loss / args.gradient_accumulation_steps

      if args.fp16:
        with amp.scale_loss(loss, optimizer) as scaled_loss:
          scaled_loss.backward()
      else:
        loss.backward()

      tr_loss += loss.item()
      if (step + 1) % args.gradient_accumulation_steps == 0:
        if args.fp16:
          torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
        else:
          torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

        optimizer.step()
        scheduler.step()  # Update learning rate schedule
        model.zero_grad()
        global_step += 1

        # Log metrics
        if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
          # Only evaluate when single GPU otherwise metrics may not average well
          if args.local_rank == -1 and args.evaluate_during_training:
            results = evaluate(args, model, tokenizer)
            for key, value in results.items():
              tb_writer.add_scalar("eval_{}".format(key), value, global_step)
          tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
          tb_writer.add_scalar("loss", (tr_loss - logging_loss) / args.logging_steps, global_step)
          logging_loss = tr_loss

        # Save model checkpoint
        if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
          output_dir = os.path.join(args.output_dir, "checkpoint-{}".format(global_step))
          if not os.path.exists(output_dir):
            os.makedirs(output_dir)
          # Take care of distributed/parallel training
          model_to_save = model.module if hasattr(model, "module") else model
          model_to_save.save_pretrained(output_dir)
          tokenizer.save_pretrained(output_dir)

          torch.save(args, os.path.join(output_dir, "training_args.bin"))
          logger.info("Saving model checkpoint to %s", output_dir)

          torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
          torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
          logger.info("Saving optimizer and scheduler states to %s", output_dir)

      if args.max_steps > 0 and global_step > args.max_steps:
        epoch_iterator.close()
        break
    if args.max_steps > 0 and global_step > args.max_steps:
      train_iterator.close()
      break

  if args.local_rank in [-1, 0]:
    tb_writer.close()

  return global_step, tr_loss / global_step


def evaluate(args, model, tokenizer, prefix=""):
  dataset, examples, features = load_and_cache_examples(args, tokenizer, evaluate=True, output_examples=True)

  if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
    os.makedirs(args.output_dir)

  args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)

  # Note that DistributedSampler samples randomly
  eval_sampler = SequentialSampler(dataset)
  eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

  # multi-gpu evaluate
  if args.n_gpu > 1 and not isinstance(model, torch.nn.DataParallel):
    model = torch.nn.DataParallel(model)

  # Eval!
  logger.info("***** Running evaluation {} *****".format(prefix))
  logger.info("  Num examples = %d", len(dataset))
  logger.info("  Batch size = %d", args.eval_batch_size)

  all_results = []
  start_time = timeit.default_timer()

  for batch in tqdm(eval_dataloader, desc="Evaluating"):
    model.eval()
    batch = tuple(t.to(args.device) for t in batch)

    with torch.no_grad():
      inputs = {
        "input_ids": batch[0],
        "attention_mask": batch[1],
        "token_type_ids": batch[2],
      }

      if args.model_type in ["xlm", "roberta", "distilbert", "camembert"]:
        del inputs["token_type_ids"]

      feature_indices = batch[3]

      # XLNet and XLM use more arguments for their predictions
      if args.model_type in ["xlnet", "xlm"]:
        inputs.update({"cls_index": batch[4], "p_mask": batch[5]})
        # for lang_id-sensitive xlm models
        if hasattr(model, "config") and hasattr(model.config, "lang2id"):
          inputs.update(
            {"langs": (torch.ones(batch[0].shape, dtype=torch.int64) * args.lang_id).to(args.device)}
          )

      outputs = model(**inputs)

    for i, feature_index in enumerate(feature_indices):
      # TODO: i and feature_index are the same number! Simplify by removing enumerate?
      eval_feature = features[feature_index.item()]
      unique_id = int(eval_feature.unique_id)

      output = [to_list(output[i]) for output in outputs]

      # Some models (XLNet, XLM) use 5 arguments for their predictions, while the other "simpler"
      # models only use two.
      if len(output) >= 5:
        start_logits = output[0]
        start_top_index = output[1]
        end_logits = output[2]
        end_top_index = output[3]
        cls_logits = output[4]

        result = SquadResult(
          unique_id,
          start_logits,
          end_logits,
          start_top_index=start_top_index,
          end_top_index=end_top_index,
          cls_logits=cls_logits,
        )

      else:
        start_logits, end_logits = output
        result = SquadResult(unique_id, start_logits, end_logits)

      all_results.append(result)

  evalTime = timeit.default_timer() - start_time
  logger.info("  Evaluation done in total %f secs (%f sec per example)", evalTime, evalTime / len(dataset))

  # Compute predictions
  output_prediction_file = os.path.join(args.output_dir, "predictions_{}.json".format(prefix))
  output_nbest_file = os.path.join(args.output_dir, "nbest_predictions_{}.json".format(prefix))

  if args.version_2_with_negative:
    output_null_log_odds_file = os.path.join(args.output_dir, "null_odds_{}.json".format(prefix))
  else:
    output_null_log_odds_file = None

  # XLNet and XLM use a more complex post-processing procedure
  if args.model_type in ["xlnet", "xlm"]:
    start_n_top = model.config.start_n_top if hasattr(model, "config") else model.module.config.start_n_top
    end_n_top = model.config.end_n_top if hasattr(model, "config") else model.module.config.end_n_top

    predictions = compute_predictions_log_probs(
      examples,
      features,
      all_results,
      args.n_best_size,
      args.max_answer_length,
      output_prediction_file,
      output_nbest_file,
      output_null_log_odds_file,
      start_n_top,
      end_n_top,
      args.version_2_with_negative,
      tokenizer,
      args.verbose_logging,
    )
  else:
    predictions = compute_predictions_logits(
      examples,
      features,
      all_results,
      args.n_best_size,
      args.max_answer_length,
      args.do_lower_case,
      output_prediction_file,
      output_nbest_file,
      output_null_log_odds_file,
      args.verbose_logging,
      args.version_2_with_negative,
      args.null_score_diff_threshold,
      tokenizer,
    )

  # Compute the F1 and exact scores.
  results = squad_evaluate(examples, predictions)
  return results


def load_and_cache_examples(args, tokenizer, evaluate=False, output_examples=False):
  if args.local_rank not in [-1, 0] and not evaluate:
    # Make sure only the first process in distributed training process the dataset, and the others will use the cache
    torch.distributed.barrier()

  # Load data features from cache or dataset file
  input_dir = args.data_dir if args.data_dir else "."
  cached_features_file = os.path.join(
    input_dir,
    "cached_{}_{}_{}".format(
      "dev" if evaluate else "train",
      list(filter(None, args.model_name_or_path.split("/"))).pop(),
      str(args.max_seq_length),
    ),
  )

  # Init features and dataset from cache if it exists
  if os.path.exists(cached_features_file) and not args.overwrite_cache:
    logger.info("Loading features from cached file %s", cached_features_file)
    features_and_dataset = torch.load(cached_features_file)
    features, dataset, examples = (
      features_and_dataset["features"],
      features_and_dataset["dataset"],
      features_and_dataset["examples"],
    )
  else:
    logger.info("Creating features from dataset file at %s", input_dir)

    if not args.data_dir and ((evaluate and not args.predict_file) or (not evaluate and not args.train_file)):
      try:
        import tensorflow_datasets as tfds
      except ImportError:
        raise ImportError("If not data_dir is specified, tensorflow_datasets needs to be installed.")

      if args.version_2_with_negative:
        logger.warn("tensorflow_datasets does not handle version 2 of SQuAD.")

      tfds_examples = tfds.load("squad")
      examples = SquadV1Processor().get_examples_from_dataset(tfds_examples, evaluate=evaluate)
    else:
      processor = SquadV2Processor() if args.version_2_with_negative else SquadV1Processor()
      if evaluate:
        examples = processor.get_dev_examples(args.data_dir, filename=args.predict_file)
      else:
        examples = processor.get_train_examples(args.data_dir, filename=args.train_file)

    features, dataset = squad_convert_examples_to_features(
      examples=examples,
      tokenizer=tokenizer,
      max_seq_length=args.max_seq_length,
      doc_stride=args.doc_stride,
      max_query_length=args.max_query_length,
      is_training=not evaluate,
      return_dataset="pt",
      threads=args.threads,
    )

    if args.local_rank in [-1, 0]:
      logger.info("Saving features into cached file %s", cached_features_file)
      torch.save({"features": features, "dataset": dataset, "examples": examples}, cached_features_file)

  if args.local_rank == 0 and not evaluate:
    # Make sure only the first process in distributed training process the dataset, and the others will use the cache
    torch.distributed.barrier()

  if output_examples:
    return dataset, examples, features
  return dataset


def main():
  parser = get_parser(MODEL_TYPES, ALL_MODELS)
  args = parser.parse_args()

  if args.doc_stride >= args.max_seq_length - args.max_query_length:
    logger.warning(
      "WARNING - You've set a doc stride which may be superior to the document length in some "
      "examples. This could result in errors when building features from the examples. Please reduce the doc "
      "stride or increase the maximum length to ensure the features are correctly built."
    )

  if (
    os.path.exists(args.output_dir)
    and os.listdir(args.output_dir)
    and args.do_train
    and not args.overwrite_output_dir
  ):
    raise ValueError(
      "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
        args.output_dir
      )
    )

  # Setup distant debugging if needed
  if args.server_ip and args.server_port:
    # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
    import ptvsd

    print("Waiting for debugger attach")
    ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
    ptvsd.wait_for_attach()

  # Setup CUDA, GPU & distributed training
  if args.local_rank == -1 or args.no_cuda:
    device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
    args.n_gpu = 0 if args.no_cuda else torch.cuda.device_count()
  else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
    torch.cuda.set_device(args.local_rank)
    device = torch.device("cuda", args.local_rank)
    torch.distributed.init_process_group(backend="nccl")
    args.n_gpu = 1
  args.device = device

  # Setup logging
  logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
  )
  logger.warning(
    "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
    args.local_rank,
    device,
    args.n_gpu,
    bool(args.local_rank != -1),
    args.fp16,
  )

  # Set seed
  set_seed(args)

  # Load pretrained model and tokenizer
  if args.local_rank not in [-1, 0]:
    # Make sure only the first process in distributed training will download model & vocab
    torch.distributed.barrier()

  args.model_type = args.model_type.lower()
  config = AutoConfig.from_pretrained(
    args.config_name if args.config_name else args.model_name_or_path,
    cache_dir=args.cache_dir if args.cache_dir else None,
    output_attentions = args.uncertainty_model
  )
  tokenizer = AutoTokenizer.from_pretrained(
    args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
    do_lower_case=args.do_lower_case,
    cache_dir=args.cache_dir if args.cache_dir else None,
  )
  model = AutoModelForQuestionAnswering.from_pretrained(
    args.model_name_or_path,
    from_tf=bool(".ckpt" in args.model_name_or_path),
    config=config,
    cache_dir=args.cache_dir if args.cache_dir else None,
  )

  if args.uncertainty_model:
    uncertainty_inp_size = config.num_hidden_layers * config.num_attention_heads
    uncertainty_model = torch.nn.Sequential(
      torch.nn.Linear(uncertainty_inp_size, uncertainty_inp_size//2),
      torch.nn.Dropout(0.1),
      torch.nn.ReLU(),
      torch.nn.Dropout(0.1),
      torch.nn.Linear(uncertainty_inp_size//2, 2)
    )
    uncertainty_model.to(args.device)

  if args.local_rank == 0:
    # Make sure only the first process in distributed training will download model & vocab
    torch.distributed.barrier()

  model.to(args.device)
  
  logger.info("Training/evaluation parameters %s", args)

  # Before we do anything with models, we want to ensure that we get fp16 execution of torch.einsum if args.fp16 is set.
  # Otherwise it'll default to "promote" mode, and we'll get fp32 operations. Note that running `--fp16_opt_level="O2"` will
  # remove the need for this code, but it is still valid.
  if args.fp16:
    try:
      import apex

      apex.amp.register_half_function(torch, "einsum")
    except ImportError:
      raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")

  # Training
  if args.do_train:
    train_dataset = load_and_cache_examples(args, tokenizer, evaluate=False, output_examples=False)
    global_step, tr_loss = train(args, train_dataset, model, tokenizer, uncertainty_model if args.uncertainty_model else None)
    logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

  # Save the trained model and the tokenizer
  if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
    # Create output directory if needed
    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
      os.makedirs(args.output_dir)

    logger.info("Saving model checkpoint to %s", args.output_dir)
    # Save a trained model, configuration and tokenizer using `save_pretrained()`.
    # They can then be reloaded using `from_pretrained()`
    # Take care of distributed/parallel training
    model_to_save = model.module if hasattr(model, "module") else model
    model_to_save.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)

    # Good practice: save your training arguments together with the trained model
    torch.save(args, os.path.join(args.output_dir, "training_args.bin"))

    # Load a trained model and vocabulary that you have fine-tuned
    model = AutoModelForQuestionAnswering.from_pretrained(args.output_dir)  # , force_download=True)
    tokenizer = AutoTokenizer.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)
    model.to(args.device)
    if args.uncertainty_model:
      uncertainty_model.to(args.device)

  # Evaluation - we can ask to evaluate all the checkpoints (sub-directories) in a directory
  results = {}
  if args.do_eval and args.local_rank in [-1, 0]:
    if args.do_train:
      logger.info("Loading checkpoints saved during training for evaluation")
      checkpoints = [args.output_dir]
      if args.eval_all_checkpoints:
        checkpoints = list(
          os.path.dirname(c)
          for c in sorted(glob.glob(args.output_dir + "/**/" + WEIGHTS_NAME, recursive=True))
        )
        logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)  # Reduce model loading logs
    else:
      logger.info("Loading checkpoint %s for evaluation", args.model_name_or_path)
      checkpoints = [args.model_name_or_path]

    logger.info("Evaluate the following checkpoints: %s", checkpoints)

    for checkpoint in checkpoints:
      # Reload the model
      global_step = checkpoint.split("-")[-1] if len(checkpoints) > 1 else ""
      model = AutoModelForQuestionAnswering.from_pretrained(checkpoint)  # , force_download=True)
      model.to(args.device)

      # Evaluate
      result = evaluate(args, model, tokenizer, prefix=global_step)

      result = dict((k + ("_{}".format(global_step) if global_step else ""), v) for k, v in result.items())
      results.update(result)

  logger.info("Results: {}".format(results))

  return results


if __name__ == "__main__":
  main()
