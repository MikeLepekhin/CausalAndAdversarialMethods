from argparse import ArgumentParser
from pathlib import Path
import torch
import logging
import json
import random
import numpy as np
import pandas as pd
from collections import namedtuple, defaultdict
from tempfile import TemporaryDirectory

from torch.utils.data import DataLoader, Dataset, RandomSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm

from transformers.tokenization_bert import BertTokenizer
from transformers.optimization import AdamW, get_linear_schedule_with_warmup
from Gender_Mail_Topics.lm_finetune.pregenerate_training_data import EPOCHS
from Gender_Mail_Topics.lm_finetune.bert_topics_finetune import BertForTopicTreatControlPreTraining, \
    BertForTopicTreatPreTraining
from BERT.bert_text_dataset import BertTextDataset
from utils import init_logger

from constants import RANDOM_SEED, GENDER_MAIL_TOPICS_PRETRAIN_DIR, BERT_PRETRAINED_MODEL, NUM_CPU, \
    GENDER_MAIL_TOPICS_PRETRAIN_DATA_DIR, GENDER_MAIL_TOPICS_PRETRAIN_IXT_DIR, GENDER_MAIL_DOMAINS

BATCH_SIZE = 6
FP16 = False

InputFeatures = namedtuple("InputFeatures", "input_ids input_mask lm_label_ids treatment_label control_label unique_id")

# log_format = '%(asctime)-10s: %(message)s'
# logging.basicConfig(level=logging.INFO, format=log_format)
logger = init_logger("Gender_Mail-Topics-pretraining", f"{GENDER_MAIL_TOPICS_PRETRAIN_DIR}")


def convert_example_to_features(example, tokenizer, max_seq_length):
    tokens = example["tokens"]
    masked_lm_positions = np.array([int(i) for i in example["masked_lm_positions"]])
    masked_lm_labels = example["masked_lm_labels"]
    treatment_label = int(example["treatment_label"])
    control_label = int(example["control_label"])
    unique_id = int(example["unique_id"])

    # assert len(tokens) == len(segment_ids) <= max_seq_length  # The preprocessed data should be already truncated
    input_ids = tokenizer.convert_tokens_to_ids(tokens)
    masked_label_ids = tokenizer.convert_tokens_to_ids(masked_lm_labels)

    input_array = np.zeros(max_seq_length, dtype=np.int)
    input_array[:len(input_ids)] = input_ids

    mask_array = np.zeros(max_seq_length, dtype=np.bool)
    mask_array[:len(input_ids)] = 1

    lm_label_array = np.full(max_seq_length, dtype=np.int, fill_value=BertTextDataset.MLM_IGNORE_LABEL_IDX)
    lm_label_array[masked_lm_positions] = masked_label_ids

    features = InputFeatures(input_ids=input_array,
                             input_mask=mask_array,
                             lm_label_ids=lm_label_array,
                             treatment_label=treatment_label,
                             control_label=control_label,
                             unique_id=unique_id)
    return features


class PregeneratedDataset(Dataset):
    def __init__(self, training_path, epoch, tokenizer, num_data_epochs, reduce_memory=False):
        self.vocab = tokenizer.vocab
        self.tokenizer = tokenizer
        self.epoch = epoch
        self.data_epoch = epoch % num_data_epochs
        data_file = training_path / f"{BERT_PRETRAINED_MODEL}_epoch_{self.data_epoch}.json"
        metrics_file = training_path / f"{BERT_PRETRAINED_MODEL}_epoch_{self.data_epoch}_metrics.json"
        assert data_file.is_file() and metrics_file.is_file()
        metrics = json.loads(metrics_file.read_text())
        num_samples = metrics['num_training_examples']
        seq_len = metrics['max_seq_len']
        self.temp_dir = None
        self.working_dir = None
        if reduce_memory:
            self.temp_dir = TemporaryDirectory()
            self.working_dir = Path(self.temp_dir.name)
            input_ids = np.memmap(filename=self.working_dir/'input_ids.memmap',
                                  mode='w+', dtype=np.int32, shape=(num_samples, seq_len))
            input_masks = np.memmap(filename=self.working_dir/'input_masks.memmap',
                                    shape=(num_samples, seq_len), mode='w+', dtype=np.bool)
            lm_label_ids = np.memmap(filename=self.working_dir/'lm_label_ids.memmap',
                                     shape=(num_samples, seq_len), mode='w+', dtype=np.int32)
            lm_label_ids[:] = BertTextDataset.MLM_IGNORE_LABEL_IDX
        else:
            input_ids = np.zeros(shape=(num_samples, seq_len), dtype=np.int32)
            input_masks = np.zeros(shape=(num_samples, seq_len), dtype=np.bool)
            lm_label_ids = np.full(shape=(num_samples, seq_len), dtype=np.int32, fill_value=BertTextDataset.MLM_IGNORE_LABEL_IDX)
            treatment_labels = np.zeros(shape=(num_samples,), dtype=np.int32)
            control_labels = np.zeros(shape=(num_samples,), dtype=np.int32)
            unique_ids = np.zeros(shape=(num_samples,), dtype=np.int32)
        logging.info(f"Loading training examples for epoch {epoch}")
        with data_file.open() as f:
            for i, line in enumerate(tqdm(f, total=num_samples, desc="Training examples")):
                line = line.strip()
                example = json.loads(line)
                features = convert_example_to_features(example, tokenizer, seq_len)
                input_ids[i] = features.input_ids
                input_masks[i] = features.input_mask
                lm_label_ids[i] = features.lm_label_ids
                treatment_labels[i] = features.treatment_label
                control_labels[i] = features.control_label
                unique_ids[i] = features.unique_id
        assert i == num_samples - 1  # Assert that the sample count metric was true
        logging.info("Loading complete!")
        self.num_samples = num_samples
        self.seq_len = seq_len
        self.input_ids = input_ids
        self.input_masks = input_masks
        self.lm_label_ids = lm_label_ids
        self.treatment_labels = treatment_labels
        self.control_labels = control_labels
        self.unique_ids = unique_ids

    def __len__(self):
        return self.num_samples

    def __getitem__(self, item):
        return (torch.tensor(self.input_ids[item].astype(np.int64)),
                torch.tensor(self.input_masks[item].astype(np.int64)),
                torch.tensor(self.lm_label_ids[item].astype(np.int64)),
                torch.tensor(self.treatment_labels[item].astype(np.int64)),
                torch.tensor(self.control_labels[item].astype(np.int64)),
                torch.tensor(self.unique_ids[item].astype(np.int64)))


def pretrain_on_domain(args):
    assert args.pregenerated_data.is_dir(), \
        "--pregenerated_data should point to the folder of files made by pregenerate_training_data.py!"

    samples_per_epoch = []
    for i in range(args.epochs):
        epoch_file = args.pregenerated_data / f"{BERT_PRETRAINED_MODEL}_epoch_{i}.json"
        metrics_file = args.pregenerated_data / f"{BERT_PRETRAINED_MODEL}_epoch_{i}_metrics.json"
        if epoch_file.is_file() and metrics_file.is_file():
            metrics = json.loads(metrics_file.read_text())
            samples_per_epoch.append(metrics['num_training_examples'])
        else:
            if i == 0:
                exit("No training data was found!")
            print(f"Warning! There are fewer epochs of pregenerated data ({i}) than training epochs ({args.epochs}).")
            print("This script will loop over the available data, but training diversity may be negatively impacted.")
            num_data_epochs = i
            break
    else:
        num_data_epochs = args.epochs

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logging.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if args.output_dir.is_dir() and list(args.output_dir.iterdir()):
        logging.warning(f"Output directory ({args.output_dir}) already exists and is not empty!")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    total_train_examples = 0
    for i in range(args.epochs):
        # The modulo takes into account the fact that we may loop over limited epochs of data
        total_train_examples += samples_per_epoch[i % len(samples_per_epoch)]

    num_train_optimization_steps = int(
        total_train_examples / args.train_batch_size / args.gradient_accumulation_steps)
    if args.local_rank != -1:
        num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    # Prepare model
    if args.control:
        model = BertForTopicTreatControlPreTraining.from_pretrained(args.bert_model)
    else:
        model = BertForTopicTreatPreTraining.from_pretrained(args.bert_model)
    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")
        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
         'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
    else:
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer,
                                                num_warmup_steps=args.warmup_steps,
                                                num_training_steps=num_train_optimization_steps)

    global_step = 0
    logging.info("***** Running training *****")
    logging.info(f"  Num examples = {total_train_examples}")
    logging.info("  Batch size = %d", args.train_batch_size)
    logging.info("  Num steps = %d", num_train_optimization_steps)
    model.train()
    loss_dict = defaultdict(list)
    for epoch in range(args.epochs):
        epoch_dataset = PregeneratedDataset(epoch=epoch, training_path=args.pregenerated_data, tokenizer=tokenizer,
                                            num_data_epochs=num_data_epochs, reduce_memory=args.reduce_memory)
        if args.local_rank == -1:
            train_sampler = RandomSampler(epoch_dataset)
        else:
            train_sampler = DistributedSampler(epoch_dataset)
        train_dataloader = DataLoader(epoch_dataset, sampler=train_sampler, batch_size=args.train_batch_size, num_workers=NUM_CPU)
        tr_loss = 0
        nb_tr_examples, nb_tr_steps = 0, 0
        with tqdm(total=len(train_dataloader), desc=f"Epoch {epoch}") as pbar:
            for step, batch in enumerate(train_dataloader):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, lm_label_ids, treatment_label, control_label, unique_id = batch
                if args.control:
                    outputs = model(input_ids=input_ids, attention_mask=input_mask, masked_lm_labels=lm_label_ids,
                                    topic_treat_label=treatment_label, topic_control_label=control_label)
                else:
                    outputs = model(input_ids=input_ids, attention_mask=input_mask, masked_lm_labels=lm_label_ids,
                                    topic_treat_label=treatment_label)
                loss, mlm_loss, treatment_loss, control_loss = outputs[:4]
                if n_gpu > 1:
                    loss = loss.mean() # mean() to average on multi-gpu.
                    mlm_loss = mlm_loss.mean()
                    treatment_loss = treatment_loss.mean()
                    if args.control:
                        control_loss = control_loss.mean()
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                pbar.update(1)
                mean_loss = tr_loss * args.gradient_accumulation_steps / nb_tr_steps
                pbar.set_postfix_str(f"Loss: {mean_loss:.5f}")
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    optimizer.zero_grad()
                    global_step += 1
                for i in range(unique_id.size(0)):
                    loss_dict["epoch"].append(epoch)
                    loss_dict["unique_id"].append(unique_id[i].item())
                    loss_dict["mlm_loss"].append(mlm_loss[i].item())
                    loss_dict["treatment_loss"].append(treatment_loss[i].item())
                    if args.control:
                        loss_dict["control_loss"].append(control_loss[i].item())
                        loss_dict["total_loss"].append(mlm_loss[i].item() + treatment_loss[i].item() + control_loss[i].item())
                    else:
                        loss_dict["total_loss"].append(mlm_loss[i].item() + treatment_loss[i].item())
        # Save a trained model
        if epoch < num_data_epochs and (n_gpu > 1 and torch.distributed.get_rank() == 0 or n_gpu <= 1):
            logging.info("** ** * Saving fine-tuned model ** ** * ")
            epoch_output_dir = args.output_dir / f"epoch_{epoch}"
            epoch_output_dir.mkdir(parents=True, exist_ok=True)
            model.save_pretrained(epoch_output_dir)
            tokenizer.save_pretrained(epoch_output_dir)

    # Save a trained model
    if n_gpu > 1 and torch.distributed.get_rank() == 0 or n_gpu <=1:
        logging.info("** ** * Saving fine-tuned model ** ** * ")
        model.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        df = pd.DataFrame.from_dict(loss_dict)
        df.to_csv(args.output_dir/"losses.csv")


def main():
    parser = ArgumentParser()
    parser.add_argument('--pregenerated_data', type=Path, required=False)
    parser.add_argument("--output_dir", type=Path, required=False)
    parser.add_argument("--bert_model", type=str, required=False, default=BERT_PRETRAINED_MODEL,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--do_lower_case", action="store_true")
    parser.add_argument("--reduce_memory", action="store_true",
                        help="Store training data as on-disc memmaps to massively reduce memory usage")

    parser.add_argument("--epochs", type=int, default=EPOCHS, help="Number of epochs to train for")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--train_batch_size",
                        default=BATCH_SIZE,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument("--warmup_steps",
                        default=0,
                        type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--adam_epsilon",
                        default=1e-8,
                        type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--learning_rate",
                        default=3e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument('--seed',
                        type=int,
                        default=RANDOM_SEED,
                        help="random seed for initialization")
    parser.add_argument("--domain", type=str, default="mail", choices=GENDER_MAIL_DOMAINS)
    parser.add_argument("--control", action="store_true",
                        help="Use pretraining model with control task")
    args = parser.parse_args()

    logger.info(f"\nFinetuning for domain: {args.domain}")

    args.pregenerated_data = Path(GENDER_MAIL_TOPICS_PRETRAIN_DATA_DIR) / args.domain
    if args.control:
        args.output_dir = Path(GENDER_MAIL_TOPICS_PRETRAIN_IXT_DIR) / args.domain / "model_control"
    else:
        args.output_dir = Path(GENDER_MAIL_TOPICS_PRETRAIN_IXT_DIR) / args.domain / "model"

    args.fp16 = FP16
    pretrain_on_domain(args)


if __name__ == '__main__':
    main()
