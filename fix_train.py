import argparse
from os import path

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

from transformers import AutoTokenizer, AutoConfig, AutoModelForSequenceClassification, DataCollatorWithPadding, Trainer, TrainingArguments
from datasets.load import load_metric

from tqdm import tqdm
import wandb

from utils import RelationExtractionDataset, DataHelper, ConfigParser
from model.metric import compute_metrics
import os
import random
import numpy as np
import pandas as pd
import ast
import pickle


class WeightedFocalLoss(nn.Module):
    "Non weighted version of Focal Loss"

    def __init__(self, alpha=.25, gamma=2):
        super(WeightedFocalLoss, self).__init__()
        self.alpha = torch.tensor([alpha, 1 - alpha]).cuda()
        self.gamma = gamma

    def forward(self, inputs, targets):
        BCE_loss = F.binary_cross_entropy_with_logits(
            inputs, targets, reduction='none')
        targets = targets.type(torch.long)
        at = self.alpha.gather(0, targets.data.view(-1))
        pt = torch.exp(-BCE_loss)
        F_loss = at * (1 - pt)**self.gamma * BCE_loss
        return F_loss.mean()


class MyTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def compute_loss(self, model, inputs, return_outputs=False):
        if self.label_smoother is not None and "labels" in inputs:
            labels = inputs.pop("labels")
        else:
            labels = None
        loss_fct = WeightedFocalLoss()
        outputs = model(**inputs)
        if labels is not None:
            loss = loss_fct(outputs[0], labels)
        else:
            loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]
        return (loss, outputs) if return_outputs else loss


def evaluate(model, val_dataset, batch_size, collate_fn, device, eval_method='f1'):
    metric = load_metric(eval_method)
    dataloader = DataLoader(
        val_dataset, batch_size=batch_size, collate_fn=collate_fn, shuffle=False)

    model.eval()
    for data in tqdm(dataloader):
        data = {key: value.to(device) for key, value in data.items()}
        with torch.no_grad():
            outputs = model(**data)
        preds = torch.argmax(outputs.logits, dim=-1)
        metric.add_batch(predictions=preds, references=data['labels'])
    model.train()

    return metric.compute(average='micro')[eval_method]


def ent_preprocess(data):
    data['sentence'] = data.apply(lambda row: add_entity_tokens(
        row['sentence'], row['object_entity'], row['subject_entity']), axis=1)
    return data


def add_entity_tokens(sentence, object_entity, subject_entity):

    def entity_mapper(entity_type):
        e_map = {'PER': '인물', 'ORG': '기관', 'LOC': '지명',
                 'POH': '기타', 'DAT': '날짜', 'NOH': '수량'}
        return e_map[entity_type]

    def extract(entity):
        return int(ast.literal_eval(entity)['start_idx']), int(ast.literal_eval(entity)['end_idx']), entity_mapper(ast.literal_eval(entity)['type'])

    obj_start_idx, obj_end_idx, obj_type = extract(object_entity)
    subj_start_idx, subj_end_idx, sbj_type = extract(subject_entity)

    if obj_start_idx < subj_start_idx:
        new_sentence = sentence[:obj_start_idx] + '#' + '*' + obj_type + '*' + sentence[obj_start_idx:obj_end_idx + 1] + '#' + \
            sentence[obj_end_idx + 1:subj_start_idx] + '@' + '^' + sbj_type + '^' + sentence[subj_start_idx:subj_end_idx + 1] + \
            '@' + sentence[subj_end_idx + 1:]
    else:
        new_sentence = sentence[:subj_start_idx] + '@' + '^' + sbj_type + '^' + sentence[subj_start_idx:subj_end_idx + 1] + '@' + \
            sentence[subj_end_idx + 1:obj_start_idx] + '#' + '*' + obj_type + '*' + sentence[obj_start_idx:obj_end_idx + 1] + \
            '#' + sentence[obj_end_idx + 1:]

    return new_sentence


def tokenize(data, tokenizer):
    tokenized = tokenizer(
        data['sentence'].tolist(),
        truncation=True,
        return_token_type_ids=False,
    )
    return tokenized


def convert_labels_by_dict(labels, dictionary='data/dict_label_to_num.pkl'):
    with open(dictionary, 'rb') as f:
        dictionary = pickle.load(f)
    return np.array([dictionary[label] for label in labels])


def _preprocess(data):
    data = ent_preprocess(data)

    def extract(d): return ast.literal_eval(d)['word']

    subjects = list(map(extract, data['subject_entity']))
    objects = list(map(extract, data['object_entity']))

    _processed = pd.DataFrame({
        'id': data['id'],
        'sentence': data['sentence'],
        'subject_entity': subjects,
        'object_entity': objects,
    })
    _labels = convert_labels_by_dict(labels=data['label'])
    return _processed, _labels


def train(args):
    hp_config = ConfigParser(config=args.hp_config).config
    seed_everything(hp_config['seed'])

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    data_collator = DataCollatorWithPadding(tokenizer=tokenizer)

    model_config = AutoConfig.from_pretrained(args.model_name)
    model_config.num_labels = 30

    if args.disable_wandb == True:
        os.environ["WANDB_DISABLED"] = "true"
    else:
        wandb.login()

    val_scores = []

    pd_train = pd.read_csv('data/yohai_target_augmented.csv')
    pd_valid = pd.read_csv('data/valid_10.csv')

    if args.aug_data_dir:
        pd_aug = pd.read_csv(args.aug_data_dir)
        pd_train = pd.concat([pd_train, pd_aug])

    train_data, train_labels = _preprocess(pd_train)
    val_data, val_labels = _preprocess(pd_valid)

    train_data = tokenize(train_data, tokenizer=tokenizer)
    val_data = tokenize(val_data, tokenizer=tokenizer)

    train_dataset = RelationExtractionDataset(
        train_data, labels=train_labels)
    val_dataset = RelationExtractionDataset(val_data, labels=val_labels)

    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name, config=model_config)
    model.to(device)

    if args.disable_wandb == False:
        wandb.init(
            project='klue',
            entity='chungye-mountain-sherpa',
            name=args.aug_data_dir.split('.')[0],
            group='yohan-aug-test/' + args.model_name.split('/')[-1]
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
            metric_for_best_model='micro f1 score'
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
            load_best_model_at_end=True,
            metric_for_best_model='micro f1 score',
        )

    # trainer = Trainer(
    trainer = MyTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=val_dataset,
        compute_metrics=compute_metrics,
        data_collator=data_collator
    )
    trainer.train()
    model.save_pretrained(args.save_dir)

    # score = evaluate(
    #     model=model,
    #     val_dataset=val_dataset,
    #     batch_size=hp_config['batch_size'],
    #     collate_fn=data_collator,
    #     device=device
    # )
    # val_scores.append(score)

    # if args.disable_wandb == False:
    #     wandb.log({'fold': score})
    #     wandb.finish()


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
                        default='hp_config/roberta_small.json')

    # parser.add_argument('--data_dir', type=str, default='data/cleaned_target_augmented.csv')

    # parser.add_argument('--data_dir', type=str, default='data/train_10_change_label_entities_dataset.csv')
    parser.add_argument('--data_dir', type=str, default='data/train.csv')
    parser.add_argument('--aug_data_dir', type=str, default='')
    parser.add_argument('--output_dir', type=str,
                        default='./roberta_small_experiment')
    parser.add_argument('--logging_dir', type=str, default='./logs')
    parser.add_argument('--save_dir', type=str,
                        default='./roberta_small_experiment')

    parser.add_argument('--model_name', type=str, default='klue/roberta-small')
    parser.add_argument('--mode', type=str, default='plain',
                        choices=['plain', 'skf'])
    parser.add_argument('--split_ratio', type=float, default=0.0)
    parser.add_argument('--n_splits', type=int, default=5)
    parser.add_argument('--eval_strategy', type=str,
                        default='epoch', choices=['steps', 'epoch'])
    parser.add_argument('--add_ent_token', type=bool, default=True)
    parser.add_argument('--disable_wandb', type=bool, default=False)

    args = parser.parse_args()

    train(args=args)
