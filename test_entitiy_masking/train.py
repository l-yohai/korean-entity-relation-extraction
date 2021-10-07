import argparse
from os import path

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from transformers import AutoConfig, AutoTokenizer, DataCollatorWithPadding, Trainer, TrainingArguments, EarlyStoppingCallback, AutoModelForSequenceClassification
from datasets.load import load_metric

from tqdm import tqdm
import wandb

from utils import RelationExtractionDataset, DataHelper, ConfigParser
from metric import compute_metrics
import os
import random
import numpy as np
import pandas as pd
from loss_func import *

from custom_model import RBERT


def evaluate(model, val_dataset, batch_size, device, eval_method='f1'):
    metric = load_metric(eval_method)
    dataloader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False)

    model.eval()
    for data in tqdm(dataloader):
        data = {key: value.to(device) for key, value in data.items()}
        with torch.no_grad():
            outputs = model(**data)
        preds = torch.argmax(outputs.logits, dim=-1)
        metric.add_batch(predictions=preds, references=data['labels'])
    model.train()

    return metric.compute(average='micro')[eval_method]


class MyTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        outputs = model(**inputs)
        logits = outputs.get('logits')

        loss_type = "focal"
        beta = 0.9999
        gamma = 2.0

        criterion = CB_loss(beta, gamma)
        if torch.cuda.is_available():
            criterion.cuda()
        loss_fct = criterion(logits, labels, loss_type)

        return (loss_fct, outputs) if return_outputs else loss_fct


def train(args):
    hp_config = ConfigParser(config=args.hp_config).config
    seed_everything(hp_config['seed'])

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)

    model_config = AutoConfig.from_pretrained(args.model_name)
    model_config.num_labels = 30

    if args.disable_wandb == True:
        os.environ["WANDB_DISABLED"] = "true"
    else:
        wandb.login()

    val_scores = []
    helper = DataHelper(data_dir=args.data_dir,
                        add_ent_token=args.add_ent_token)
    # 추가
    #entity_data = pd.read_csv('/opt/ml/dataset/train/entity_train.csv')
    #feature = entity_data.columns
    ###
    all_data = pd.read_csv(args.data_dir)
    aug_data = pd.read_csv('/opt/ml/dataset/train/aug_all.csv')

    for k, (train_idxs, val_idxs) in enumerate(helper.split(ratio=args.split_ratio, n_splits=args.n_splits, mode=args.mode, random_seed=hp_config['seed'])):
        #train_data, train_labels = helper.from_idxs(idxs=train_idxs)
        train_data = all_data.iloc[train_idxs]
        ###
        #train_entity = entity_data[feature].iloc[train_idxs]
        ###
        #val_data, val_labels = helper.from_idxs(idxs=val_idxs)
        val_data = all_data.iloc[val_idxs]
        val_labels = helper.convert_labels_by_dict(val_data['label'])
        ###
        #val_entity = entity_data[feature].iloc[val_idxs]
        ###
        train_data = pd.concat([train_data, aug_data])
        train_labels = helper.convert_labels_by_dict(train_data['label'])

        print(len(all_data))
        print(len(train_data))
        print(len(val_data))

        train_data = helper.tokenize(
            train_data, tokenizer=tokenizer)
        val_data = helper.tokenize(
            val_data, tokenizer=tokenizer)

        train_dataset = RelationExtractionDataset(
            train_data, labels=train_labels)
        val_dataset = RelationExtractionDataset(val_data, labels=val_labels)

        model = AutoModelForSequenceClassification.from_pretrained(
            args.model_name, config=model_config)
####
        # model.bert.resize_token_embeddings(len(tokenizer))

        # for param in model.parameters():
        #     param.requires_grad = False

        # for param in model.classifier.parameters():
        #     param.requires_grad = True

        # for param in model.roberta.encoder.layer[-1].parameters():
        #     param.requires_grad = True
        # for param in model.roberta.encoder.layer[-2].parameters():
        #     param.requires_grad = True

        model.to(device)

        if args.disable_wandb == False:
            wandb.init(
                project='klue',
                entity='chungye-mountain-sherpa',
                name=f'{args.model_name}_hk_freeze' +
                (f'fold_{k}' if args.mode == 'skf' else f'{args.mode}'),
                group='entity_embedding'
            )

        if args.eval_strategy == 'epoch':
            training_args = TrainingArguments(
                output_dir=args.output_dir,
                per_device_train_batch_size=hp_config['batch_size'],
                per_device_eval_batch_size=hp_config['batch_size'],
                gradient_accumulation_steps=hp_config['gradient_accumulation_steps'],
                learning_rate=hp_config['learning_rate'],
                weight_decay=hp_config['weight_decay'],
                num_train_epochs=hp_config['epochs'],
                logging_dir=args.logging_dir,
                logging_steps=200,
                save_total_limit=2,
                evaluation_strategy=args.eval_strategy,
                save_strategy=args.eval_strategy,
                load_best_model_at_end=True,
                # dataloader_num_workers=4,
                #metric_for_best_model='micro f1 score'
            )
        elif args.eval_strategy == 'steps':
            training_args = TrainingArguments(
                output_dir=args.output_dir,
                per_device_train_batch_size=hp_config['batch_size'],
                per_device_eval_batch_size=hp_config['batch_size'],
                gradient_accumulation_steps=hp_config['gradient_accumulation_steps'],
                learning_rate=hp_config['learning_rate'],
                weight_decay=hp_config['weight_decay'],
                num_train_epochs=hp_config['epochs'],
                logging_dir=args.logging_dir,
                logging_steps=200,
                save_total_limit=2,
                evaluation_strategy=args.eval_strategy,
                eval_steps=200,
                save_steps=200,
                # dataloader_num_workers=4,
                load_best_model_at_end=True,
                fp16=True,
                fp16_opt_level='O1'
                #metric_for_best_model='micro f1 score',
            )

        # trainer = Trainer(
        trainer = MyTrainer(
            model=model,
            args=training_args,
            train_dataset=train_dataset,
            eval_dataset=val_dataset,
            compute_metrics=compute_metrics,
            data_collator=DataCollatorWithPadding(tokenizer),
            callbacks=[EarlyStoppingCallback(early_stopping_patience=4)]
        )
        trainer.train()
        model.save_pretrained(
            path.join(args.save_dir, f'{k}_fold' if args.mode == 'skf' else args.mode))

        # score = evaluate(
        #     model=model,
        #     val_dataset=val_dataset,
        #     batch_size=hp_config['batch_size'],
        #     device=device
        # )
        # val_scores.append(score)

        # if args.disable_wandb == False:
        #     wandb.log({'fold': score})
        wandb.finish()

    if args.mode == 'skf' and args.disable_wandb == False:
        wandb.init(
            project='klue',
            entity='chungye-mountain-sherpa',
            name=f'{args.model_name}_{args.n_splits}_fold_avg',
            group='entity_embedding'
        )
        wandb.log({'fold_avg_eval': sum(val_scores) / args.n_splits})


def seed_everything(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--hp_config', type=str,
                        default='hp_config/roberta_large_entity.json')

    parser.add_argument('--data_dir', type=str,
                        default='/opt/ml/dataset/train/train.csv')
    parser.add_argument('--output_dir', type=str,
                        default='./results')
    parser.add_argument('--logging_dir', type=str, default='./logs')
    parser.add_argument('--save_dir', type=str,
                        default='./best_model')

    parser.add_argument('--model_name', type=str, default='klue/roberta-large')
    parser.add_argument('--mode', type=str, default='skf',
                        choices=['plain', 'skf'])
    parser.add_argument('--split_ratio', type=float, default=0.1)
    parser.add_argument('--n_splits', type=int, default=5)
    parser.add_argument('--eval_strategy', type=str,
                        default='steps', choices=['steps', 'epoch'])
    parser.add_argument('--add_ent_token', type=bool, default=False)
    parser.add_argument('--disable_wandb', type=bool, default=False)

    args = parser.parse_args()

    train(args=args)