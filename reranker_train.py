# -*- coding: utf-8 -*-
"""LECR-v17c-train.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1nb2iJ7zAHU5VmZ_pYKHUQX7lEgx45yxo

# Version v17c
* Arcface + Second stage + XGBoost

# Packages
"""

import os, gc, math, random, pickle, json, time
import numpy as np
import pandas as pd
from tqdm.notebook import tqdm
tqdm.pandas()

from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import StratifiedGroupKFold, GroupKFold
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.pipeline import Pipeline
from sklearn.neighbors import NearestNeighbors

from scipy.spatial.distance import cdist

import torch
from torch import nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torch.cuda.amp import autocast, GradScaler

import transformers
from transformers import AutoTokenizer, AutoConfig, AutoModel
from transformers import get_cosine_schedule_with_warmup, get_linear_schedule_with_warmup, AdamW
transformers.logging.set_verbosity_error()

import warnings
warnings.filterwarnings('ignore')

os.environ['TOKENIZERS_PARALLELISM'] = 'true'

"""# Initial settings"""

class Config(object):
    # General setting
    competition_name = 'LECR'    # Learning Equality - Curriculum Recommendations
    seed = 2022
    env = 'vastai'
    ver = 'v17c'
    if env == 'colab':
        from google.colab import drive
        drive.mount('/content/drive')
    mode = 'train'
    use_tqdm = True
    use_log = True
    debug = False
    device = torch.device('cuda:1' if torch.cuda.is_available() else 'cpu')
    # Encoding model
    backbone = 'paraphrase-multilingual-mpnet-base-v4'    # 'microsoft/mdeberta-v3-base', 'xlm-roberta-base'
    tokenizer = AutoTokenizer.from_pretrained(backbone)
    config = AutoConfig.from_pretrained(backbone)
    done_embedding = False
    # Add new token
    sep_token = '[LECR]'
    sep_token_id = tokenizer.vocab_size + 1
    special_tokens_dict = {'additional_special_tokens': [sep_token]}
    tokenizer.add_special_tokens(special_tokens_dict)
    # Embedding model
    embedding_model = 'v17a'
    # Data
    done_kfold_split = True
    nfolds = 5
    negative_sample_ratio = 0.3
    done_context = True
    # Dataloader
    max_len = 128
    batch_size = 64 if not debug else 4
    num_workers = os.cpu_count()
    # For validation
    thres = {
        'cosine': None,
        'num_k': 50,
    }
    
    ################## For the second-stage training ##################
    apex = True
    gradient_checkpointing = False
    stg2_nepochs = 3
    gradient_accumulation_steps = 1
    max_grad_norm = 50
    # Optimizer
    lr = 1e-5
    weight_decay = 1e-2
    encoder_lr = 1e-5
    decoder_lr = 1e-3
    min_lr = 1e-6
    eps = 1e-6
    betas = (0.9, 0.999)
    training_folds = 'None'
    # Scheduler
    scheduler_type = 'cosine'    # 'linear', 'cosine'
    if scheduler_type == 'cosine':
        num_cycles = 0.5
    num_warmup_steps = 0.
    batch_scheduler = True
    
    # Paths
    if env == 'colab':
        comp_data_dir = f'/content/drive/My Drive/Kaggle competitions/{competition_name}/comp_data'
        ext_data_dir = f'/content/drive/My Drive/Kaggle competitions/{competition_name}/ext_data'
        model_dir = f'/content/drive/My Drive/Kaggle competitions/{competition_name}/model'
        embedding_model_dir = f'{model_dir}/{embedding_model[:-1]}/{embedding_model[-1]}'
        os.makedirs(os.path.join(model_dir, ver[:-1], ver[-1]), exist_ok = True)
    elif env == 'kaggle':
        comp_data_dir = ...
        ext_data_dir = ...
        model_dir = ...
    elif env == 'vastai':
        comp_data_dir = 'data'
        ext_data_dir = 'ext_data'
        model_dir = f'model'
        embedding_model_dir = f'{model_dir}/{embedding_model[:-1]}/{embedding_model[-1]}'
        os.makedirs(os.path.join(model_dir, ver[:-1], ver[-1]), exist_ok = True)

cfg = Config()

"""# Set seed"""

def set_random_seed(seed, use_cuda = True):
    np.random.seed(seed) # cpu vars
    torch.manual_seed(seed) # cpu  vars
    random.seed(seed) # Python
    os.environ['PYTHONHASHSEED'] = str(seed) # Python hash building
    if use_cuda:
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed) # gpu vars
        torch.backends.cudnn.deterministic = True  #needed
        torch.backends.cudnn.benchmark = False

set_random_seed(cfg.seed)

"""# Prepare log"""

if cfg.use_log:
    import logging
    from imp import reload
    reload(logging)
    logging.basicConfig(
        level = logging.INFO,
        format = '%(asctime)s %(message)s',
        datefmt = '%H:%M:%S',
        handlers = [
            logging.FileHandler(f"train_{cfg.ver}_{time.strftime('%m%d_%H%M', time.localtime())}_seed_{cfg.seed}_folds_{''.join([str(i) for i in cfg.training_folds])}.log"),
            logging.StreamHandler()
        ]
    )

    logging.info(
        '\nver: {}\n'
        'backbone: {}\n'
        'env: {}\n'
        'seed: {}\n'
        'training_folds: {}\n'
        'max_len: {}\n'
        'batch_size: {}\n'
        'num_workers: {}\n'
        'stage-2 nepochs: {}\n'
        'lr: {}\n'
        'weight_decay: {}\n'.format(cfg.ver, cfg.backbone, cfg.env, cfg.seed, cfg.training_folds, 
                                       cfg.max_len, cfg.batch_size, cfg.num_workers, cfg.stg2_nepochs, 
                                       cfg.lr, cfg.weight_decay)
    )

def print_log(cfg, message):
    if cfg.use_log:
        logging.info(message)
    else:
        print(message)

"""# Explore the data"""

content_df = pd.read_csv(os.path.join(cfg.comp_data_dir, 'content.csv'))
topics_df = pd.read_csv(os.path.join(cfg.comp_data_dir, 'topics.csv'))

all_languages = sorted(list(set(topics_df['language'].tolist() + content_df['language'].tolist())))
cfg.languages_map = dict(zip(all_languages, range(len(all_languages))))

"""# K-fold spliting"""

if not cfg.done_kfold_split:
    kf = StratifiedGroupKFold(n_splits = cfg.nfolds)
    folds = list(kf.split(topics_df, y = topics_df['has_content'], groups = topics_df['channel']))
    topics_df['fold'] = -1

    for fold, (train_idx, val_idx) in enumerate(folds):
        topics_df.loc[val_idx, 'fold'] = fold
    topic2fold_split = topics_df[['id', 'fold']].set_index('id').to_dict()['fold']

    with open(os.path.join(cfg.ext_data_dir, f'topic2fold_{cfg.nfolds}split_stratifiedkfold.pkl'), 'wb') as f:
        pickle.dump(topic2fold_split, f)
else:
    with open(os.path.join(cfg.ext_data_dir, f'topic2fold_{cfg.nfolds}split_stratifiedkfold.pkl'), 'rb') as f:
        topic2fold_split = pickle.load(f)

correlations_df = pd.read_csv(os.path.join(cfg.comp_data_dir, 'correlations.csv'))
topics_df['fold'] = topics_df['id'].map(topic2fold_split)
correlations_df['fold'] = correlations_df['topic_id'].map(topic2fold_split)
content_df['fold'] = -1

for fold in range(cfg.nfolds):
    content_in_fold = set(correlations_df.loc[correlations_df['fold'] == fold, 'content_ids'].str.split().explode().tolist())
    content_not_in_fold = set(correlations_df.loc[correlations_df['fold'] != fold, 'content_ids'].str.split().explode().tolist())
    only_content_in_fold = content_in_fold - content_not_in_fold
    content_df.loc[content_df['id'].isin(only_content_in_fold), 'fold'] = fold

"""# Prepare the data

* Process the topic and content data
"""

def process_data(cfg, df, is_content = False):
    # Fill NaN values in the title and description columns
    df['title'] = df['title'].fillna(' ')
    df['description'] = df['description'].fillna(' ')
    if is_content:
        df['text'] = df['text'].fillna(' ')

    # Encode the language
    df['encoded_language'] = df['language'].map(cfg.languages_map)
    return df

topics_df = process_data(cfg, topics_df)
content_df = process_data(cfg, content_df, is_content = True)

"""# Helper functions from the host"""

class Topic:
    def __init__(self, topic_id):
        self.id = topic_id

    @property
    def parent(self):
        parent_id = topics_df.loc[self.id].parent
        if pd.isna(parent_id):
            return None
        else:
            return Topic(parent_id)

    @property
    def ancestors(self):
        ancestors = []
        parent = self.parent
        while parent is not None:
            ancestors.append(parent)
            parent = parent.parent
        return ancestors

    @property
    def siblings(self):
        if not self.parent:
            return []
        else:
            return [topic for topic in self.parent.children if topic != self]

    @property
    def content(self):
        if self.id in correlations_df.index:
            return [ContentItem(content_id) for content_id in correlations_df.loc[self.id].content_ids.split()]
        else:
            return tuple([]) if self.has_content else []

    def get_breadcrumbs(self, separator=" >> ", include_self=True, include_root=True):
        ancestors = self.ancestors
        if include_self:
            ancestors = [self] + ancestors
        if not include_root:
            ancestors = ancestors[:-1]
        return separator.join([a.input_text for a in ancestors])
    
    def get_near_ancestors(self, separator=" >> ", how_near = 1, include_self = True):
        ancestors = self.ancestors
        if include_self:
            ancestors = [self] + ancestors[:how_near]
        return separator.join([a.input_text for a in ancestors])

    @property
    def children(self):
        return [Topic(child_id) for child_id in topics_df[topics_df.parent == self.id].index]

    def subtree_markdown(self, depth=0):
        markdown = "  " * depth + "- " + self.title + "\n"
        for child in self.children:
            markdown += child.subtree_markdown(depth=depth + 1)
        for content in self.content:
            markdown += ("  " * (depth + 1) + "- " + "[" + content.kind.title() + "] " + content.title) + "\n"
        return markdown

    def __eq__(self, other):
        if not isinstance(other, Topic):
            return False
        return self.id == other.id

    def __getattr__(self, name):
        return topics_df.loc[self.id][name]

    def __str__(self):
        return self.title
    
    def __repr__(self):
        return f"<Topic(id={self.id}, title=\"{self.title}\")>"


class ContentItem:
    def __init__(self, content_id):
        self.id = content_id

    @property
    def topics(self):
        return [Topic(topic_id) for topic_id in topics_df.loc[correlations_df[correlations_df.content_ids.str.contains(self.id)].index].index]

    def __getattr__(self, name):
        return content_df.loc[self.id][name]

    def __str__(self):
        return self.title
    
    def __repr__(self):
        return f"<ContentItem(id={self.id}, title=\"{self.title}\")>"

    def __eq__(self, other):
        if not isinstance(other, ContentItem):
            return False
        return self.id == other.id

    def get_all_breadcrumbs(self, separator=" >> ", include_root=True):
        breadcrumbs = []
        for topic in self.topics:
            new_breadcrumb = topic.get_breadcrumbs(separator=separator, include_root=include_root)
            if new_breadcrumb:
                new_breadcrumb = new_breadcrumb + separator + self.title
            else:
                new_breadcrumb = self.title
            breadcrumbs.append(new_breadcrumb)
        return breadcrumbs

"""# Process the topics_df data"""

topics_df = topics_df.set_index('id')
content_df = content_df.set_index('id')
correlations_df = correlations_df.set_index('topic_id')

topics_df['input_text'] = topics_df.apply(lambda x: cfg.sep_token.join([x['language'],
                                                                        x['title'],
                                                                        x['description']]), axis = 1)

content_df['input_text'] = content_df.apply(lambda x: cfg.sep_token.join([x['language'],
                                                                          x['title'],
                                                                          x['description'],
                                                                          x['text'],
                                                                          x['kind']]), axis = 1)

topics_df['text'] = topics_df['input_text'].values

for topic_id in tqdm(topics_df.index):
    topics_df.loc[topic_id, 'text'] = Topic(topic_id).get_breadcrumbs(separator = cfg.tokenizer.sep_token)
    
topics_df = topics_df.drop('input_text', axis = 1).rename(columns = {'text': 'input_text'})
    
topics_df = topics_df.reset_index()
content_df = content_df.reset_index()
correlations_df = correlations_df.reset_index()

"""# Design the dataloader"""

class LECRDataset(Dataset):
    def __init__(self, cfg, df):
        self.cfg = cfg
        self.input_text = df['input_text'].tolist()
        self.ids = df['id'].tolist()
        self.language = df['encoded_language'].tolist()
        
    def _tokenize(self, text):
        token = self.cfg.tokenizer(text,
                                   padding = 'max_length',
                                   max_length = cfg.max_len,
                                   truncation = True,
                                   return_attention_mask = True)
        return token

    def __len__(self):
        return len(self.input_text)

    def __getitem__(self, idx):
        ids = self.ids[idx]
        input_text = self.input_text[idx]
        language = self.language[idx]
        
        content_token = self._tokenize(input_text)
        
        return {
            'ids': ids,
            'input_ids': torch.tensor(content_token['input_ids'], dtype = torch.long),
            'attention_mask': torch.tensor(content_token['attention_mask'], dtype = torch.long),
            'language': torch.tensor(language, dtype = torch.long),
        }

"""# Deriving the text features"""

class TextEmbedding(object):
    def __init__(self, cfg, df):
        self.cfg = cfg
        self.df = df
        
    def _prepare_materials(self):
        print_log(self.cfg, 'Preparing the dataloader...')
        dataset = LECRDataset(cfg, self.df)
        dataloader = DataLoader(dataset, batch_size = cfg.batch_size, num_workers = cfg.num_workers, shuffle = False)
        
        print_log(self.cfg, 'Preparing the encoding model...')
        
        model = AutoModel.from_pretrained(self.cfg.embedding_model_dir).to(self.cfg.device)
        model.resize_token_embeddings(len(self.cfg.tokenizer))
        return model, dataloader
    
    def _pooler(self, x, mask = None):
        if mask is not None:
            x = x * mask.unsqueeze(-1)
            return x.sum(dim = 1) / mask.sum(dim = -1, keepdims = True)
        else:
            return x.mean(dim = 1)
    
    def _embedding(self, model, dataloader):
        model.eval()
    
        ids = []
        embeddings = []
        languages = []
        
        if self.cfg.use_tqdm:
            tbar = tqdm(dataloader)
        else:
            tbar = dataloader

        for i, item in enumerate(tbar):
            batch_ids = item['ids']
            input_ids = item['input_ids'].to(self.cfg.device)
            attention_mask = item['attention_mask'].to(self.cfg.device)
            batch_languages = item['language']
            
            with torch.no_grad():
                with autocast(enabled = self.cfg.apex):
                    local_len = max(attention_mask.sum(axis = 1))
                    batch_embedding = model(input_ids[:,:local_len], attention_mask[:,:local_len]).last_hidden_state
                    batch_embedding = self._pooler(batch_embedding, mask = attention_mask[:,:local_len])
                        
            ids.append(batch_ids)
            embeddings.append(batch_embedding.cpu().numpy())
            languages.append(batch_languages.numpy())

        ids = np.concatenate(ids)
        embeddings = np.concatenate(embeddings)
        languages = np.concatenate(languages)
        return ids, embeddings, languages

    def fit(self):
        model, dataloader = self._prepare_materials()
        ids, embeddings, languages = self._embedding(model, dataloader)
        return ids, embeddings, languages

"""* Deriving topic/content embeddings"""

topic_embeddings_object = TextEmbedding(cfg, topics_df)
topic_ids, topic_embeddings, topic_languages = topic_embeddings_object.fit()

content_embeddings_object = TextEmbedding(cfg, content_df)
content_ids, content_embeddings, content_languages = content_embeddings_object.fit()

"""# Find the candidates by k-Nearest-Neighbor algorithm

* Utils
The following function allows us to find the similar contents of a small chunk of topics, for memory management purpose
"""

def find_similar_contents(cfg, sub_topic_ids, sub_topic_embeddings, sub_topic_languages, content_ids, content_embeddings, content_languages):
    similar_languages = (sub_topic_languages.view(-1, 1) - content_languages.view(1, -1)).bool().to(cfg.device)
    cosine_similarity = F.normalize(sub_topic_embeddings.to(cfg.device)) @ F.normalize(content_embeddings.to(cfg.device)).t()
    cosine_similarity = cosine_similarity.masked_fill(similar_languages, -1)
    sorted_cosine_similarity, sorted_idx = torch.sort(cosine_similarity, dim = 1, descending = True)
    
    sorted_cosine_similarity = sorted_cosine_similarity.detach().cpu().numpy()
    sorted_idx = sorted_idx.detach().cpu().numpy()
    
    topic_dict_ids = {}
    topic_dict_distance = {}
    for i, topic_id in enumerate(sub_topic_ids):
        sorted_score = sorted_cosine_similarity[i]
        sorted_content_ids = content_ids[sorted_idx[i]]
        
        # Chose the contents with higher cosine similarity score than the cosine threshold and the same language as the topic, moreover the list is not longer than num_k threshold
        if cfg.thres['cosine'] is not None:
            if cfg.thres['num_k'] is not None:
                # If both filters are used
                chosen_content_ids = sorted_content_ids[sorted_score > cfg.thres['cosine']][:cfg.thres['num_k']]
                chosen_distance = sorted_score[sorted_score > cfg.thres['cosine']][:cfg.thres['num_k']]
            else:
                # If we filter only by the cosine similarity threshold
                chosen_content_ids = sorted_content_ids[sorted_score > cfg.thres['cosine']]
                chosen_distance = sorted_score[sorted_score > cfg.thres['cosine']]
        else:
            if cfg.thres['num_k'] is not None:
                # If we filter only by the maximum length
                chosen_content_ids = sorted_content_ids[:cfg.thres['num_k']]
                chosen_distance = sorted_score[:cfg.thres['num_k']]
            else:
                # If we don't filter at all
                chosen_content_ids = sorted_content_ids
                chosen_distance = sorted_score

        topic_dict_ids[topic_id] = ' '.join(chosen_content_ids.tolist())
        topic_dict_distance[topic_id] = chosen_distance.tolist()
    return topic_dict_ids, topic_dict_distance

"""* Find candidates"""

def find_candidates(cfg, topic_ids, topic_embeddings, topic_languages, content_ids, content_embeddings, content_languages):        
    neighbors_model = NearestNeighbors(n_neighbors = cfg.thres['num_k'], metric = 'cosine')
    neighbors_model.fit(content_embeddings)
    
    dist, indices = neighbors_model.kneighbors(topic_embeddings, return_distance = True)
    
    oof_dict_ids = {}
    oof_dict_ids_top10 = {}
    oof_dict_distance = {}
    
    for i, (topic_lang, topic_id) in enumerate(zip(topic_languages, topic_ids)):
        chosen_contents = content_ids[indices[i]]
        chosen_contents_top10 = content_ids[indices[i][:10]]
        
        oof_dict_ids[topic_id] = ' '.join(chosen_contents.tolist())
        oof_dict_ids_top10[topic_id] = ' '.join(chosen_contents_top10.tolist())
        oof_dict_distance[topic_id] = dist[i].tolist()

    # Post-process the candidate dataframe
    candidate_df = pd.DataFrame()
    candidate_df['topic_id'] = list(oof_dict_ids.keys())
    candidate_df['content_id'] = list(oof_dict_ids.values())
    candidate_df['distance'] = list(oof_dict_distance.values())
    candidate_df['content_id'] = candidate_df['content_id'].str.split()
    candidate_df = candidate_df.explode(['content_id', 'distance'])
    
    torch.cuda.empty_cache()
    
    return candidate_df

candidate_df = find_candidates(cfg, topic_ids, topic_embeddings, topic_languages, content_ids, content_embeddings, content_languages)

"""# Attach the ground truth

* Process the correlations
"""

correlations_df['content_ids'] = correlations_df['content_ids'].str.split()
correlations_df = correlations_df.explode('content_ids')
correlations_df = correlations_df.rename(columns = {'content_ids': 'content_id'})
correlations_df['label'] = 1
correlations_df

"""* Merge the candidate data and correlations data"""

data = candidate_df.merge(correlations_df, left_on = ['topic_id', 'content_id'], right_on = ['topic_id', 'content_id'], how = 'left')
data['label'] = data['label'].fillna(0)
data

data = data.merge(topics_df[['id', 'input_text', 'encoded_language', 'category', 'has_content']], 
                  left_on = 'topic_id', right_on = 'id', how = 'left').drop('id', axis = 1)
data = data.merge(content_df[['id', 'input_text', 'encoded_language']], 
                  left_on = 'content_id', right_on = 'id', suffixes = ('_t', '_c')).drop('id', axis = 1)
data = data.reset_index(drop = True)
data

def metric_fn(y_pred_ids: pd.Series, y_true_ids: pd.Series, beta = 2, eps = 1e-15):
    true_ids = y_true_ids.str.split()
    pred_ids = y_pred_ids.str.split()
    score_list = []
    for true, pred in zip(true_ids.tolist(), pred_ids.tolist()):
        TP = (set(true) & set(pred))
        precision = len(TP) / len(pred)
        recall = len(TP) / len(true)
        f2 = (1 + beta**2) * (precision * recall) / ((beta**2) * precision + recall + eps)
        score_list.append(f2)
    score = sum(score_list) / len(score_list)
    return score

def metric_eachrow_fn(pred, true, beta = 2, eps = 1e-15):
    pred = pred.split()
    true = true.split()
    TP = (set(true) & set(pred))
    precision = len(TP) / len(pred)
    recall = len(TP) / len(true)
    f2 = (1 + beta**2) * (precision * recall) / ((beta**2) * precision + recall + eps)
    return f2

data = data.sort_values(['topic_id', 'distance'])
data['cum_count'] = data.groupby('topic_id').agg('cumcount')
submission = data.loc[data['cum_count'] < 10].drop('cum_count', axis = 1)
submission = submission.groupby('topic_id')['content_id'].apply(lambda x: ' '.join(x.tolist())).to_frame().reset_index()
true = correlations_df.dropna().groupby('topic_id')['content_id'].apply(lambda x: ' '.join(x.tolist())).to_frame().reset_index()
final_oof = true.merge(submission, on = 'topic_id', how = 'left')
final_oof = final_oof.merge(topics_df[['id', 'category', 'has_content']], 
                            left_on = 'topic_id', right_on = 'id', how = 'left')
final_oof = final_oof.loc[(final_oof.category != 'source') & final_oof.has_content].drop(['id', 'category', 'has_content'], axis = 1)
final_oof.columns = ['topic_id', 'content_ids', 'pred_content_ids']

print_log(cfg, 'Score based on the k-NN algorithm:')
print_log(cfg, f"Overall score: {metric_fn(final_oof['pred_content_ids'], final_oof['content_ids'])}")

"""# The second stage

* Second stage text embedding object
"""

class SecondStageLECRDataset(Dataset):
    def __init__(self, cfg, df):
        self.cfg = cfg
        self.topic_text = df['input_text_t'].tolist()
        self.content_text = df['input_text_c'].tolist()
        
        self.topic_lang = df['encoded_language_t'].tolist()
        self.content_lang = df['encoded_language_c'].tolist()
        
        self.distance = df['distance'].tolist()
        self.label = df['label'].tolist()
        
    def _tokenize(self, text):
        token = self.cfg.tokenizer(text,
                                   padding = 'max_length',
                                   max_length = cfg.max_len,
                                   truncation = True,
                                   return_attention_mask = True)
        return token
        
    def __len__(self):
        return len(self.topic_text)
    
    def __getitem__(self, idx):
        topic_text = self.topic_text[idx]
        content_text = self.content_text[idx]
        
        topic_token = self._tokenize(topic_text)
        content_token = self._tokenize(content_text)
        
        input_ids = topic_token['input_ids'] + content_token['input_ids'][1:]    # Discard the CLS token at the beginning of the content text
        
        label = self.label[idx]
        
        return {
            'input_ids': input_ids,
            'label': label,
        }
    
class Collator(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.max_len = cfg.max_len * 2
        
    def __call__(self, batch):
        input_ids = []
        attention_mask = []
        label = []
        
        for item in batch:
            batch_input_ids = item['input_ids']
            mask = [1] * len(batch_input_ids)
            batch_label = item['label']
            
            if len(batch_input_ids) > self.max_len:
                # Truncate
                batch_input_ids = batch_input_ids[:self.max_len]
                batch_attention_mask = mask[:self.max_len]
            else:
                # Padding
                batch_input_ids = batch_input_ids + [self.cfg.tokenizer.pad_token_id] * (self.max_len - len(batch_input_ids))
                batch_attention_mask = mask + [0] * (self.max_len - len(mask))
            
            input_ids.append(batch_input_ids)
            attention_mask.append(batch_attention_mask)
            label.append(batch_label)
        
        return {
            'input_ids': torch.tensor(input_ids, dtype = torch.long),
            'attention_mask': torch.tensor(attention_mask, dtype = torch.long),
            'label': torch.tensor(label, dtype = torch.float),
        }
    
class SecondStageModel(nn.Module):
    def __init__(self, cfg):
        super(SecondStageModel, self).__init__()
        self.cfg = cfg
        self.backbone = AutoModel.from_pretrained(cfg.embedding_model_dir)
        self.backbone.resize_token_embeddings(len(cfg.tokenizer))
        self.output = nn.Linear(cfg.config.hidden_size, 1)
        
    def _pooler(self, x, mask = None):
        if mask is not None:
            x = x * mask.unsqueeze(-1)
            return x.sum(dim = 1) / mask.sum(dim = -1, keepdims = True)
        else:
            return x.mean(dim = 1)
    
    def _feature_generator(self, input_ids, attention_mask):
        local_len = max(attention_mask.sum(axis = 1))
        output_backbone = self.backbone(input_ids[:,:local_len], attention_mask[:,:local_len])
        return self._pooler(output_backbone.last_hidden_state, mask = attention_mask[:,:local_len])
    
    def loss_fn(self, output, label):
        return nn.BCEWithLogitsLoss()(output, label)
    
    def forward(self, input_ids, attention_mask, label = None):
        embedding = self._feature_generator(input_ids, attention_mask)
        output = self.output(embedding).squeeze(-1)
        
        if label is not None:
            loss = self.loss_fn(output, label)
        else:
            loss = None
            
        return loss, output, embedding
    
class SecondStageTextEmbedding(object):
    def __init__(self, cfg, df):
        self.cfg = cfg
        self.df = df
    
    def _prepare_dataloader(self, mode = 'train'):
        print_log(self.cfg, 'Preparing the dataloader...')
        dataset = SecondStageLECRDataset(cfg, self.df)
        if mode == 'train':
            dataloader = DataLoader(dataset, batch_size = self.cfg.batch_size, num_workers = self.cfg.num_workers, 
                                    shuffle = True, collate_fn = Collator(cfg))
        else:
            dataloader = DataLoader(dataset, batch_size = self.cfg.batch_size, num_workers = self.cfg.num_workers, 
                                    shuffle = False, collate_fn = Collator(cfg))
        return dataloader
    
    def _prepare_model(self):
        print_log(self.cfg, 'Preparing the second-stage model...')
        return SecondStageModel(self.cfg).to(self.cfg.device)
    
    def _prepare_optimizer(self, model):
        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_parameters = [
            {'params': [p for n, p in model.backbone.named_parameters() if not any(nd in n for nd in no_decay)],
                 'lr': cfg.encoder_lr, 'weight_decay': cfg.weight_decay},
            {'params': [p for n, p in model.backbone.named_parameters() if any(nd in n for nd in no_decay)],
                 'lr': cfg.encoder_lr, 'weight_decay': 0.0},
            {'params': [p for n, p in model.named_parameters() if 'backbone' not in n],
                 'lr': cfg.decoder_lr, 'weight_decay': 0.0}
        ]
        return AdamW(optimizer_parameters, lr = self.cfg.lr, eps = self.cfg.eps, betas = self.cfg.betas)
    
    def _prepare_scheduler(self, optimizer, num_train_steps):
        if self.cfg.scheduler_type == 'linear':
            scheduler = get_linear_schedule_with_warmup(
                optimizer, num_warmup_steps = self.cfg.num_warmup_steps, num_training_steps = num_train_steps
            )
        elif self.cfg.scheduler_type == 'cosine':
            scheduler = get_cosine_schedule_with_warmup(
                optimizer, num_warmup_steps = self.cfg.num_warmup_steps, num_training_steps = num_train_steps, 
                num_cycles = self.cfg.num_cycles
            )
        return scheduler
    
    def _prepare_materials(self):
        print_log(self.cfg, 'Preparing materials...')
        model = self._prepare_model()
        dataloader = self._prepare_dataloader()
        optimizer = self._prepare_optimizer(model)
        num_training_steps = len(dataloader) * self.cfg.stg2_nepochs
        scheduler = self._prepare_scheduler(optimizer, num_training_steps)
        return model, dataloader, optimizer, scheduler
    
    def _train_epoch(self, model, dataloader, optimizer, scheduler, return_embedding = True):
        scaler = GradScaler(enabled = self.cfg.apex)   # Enable APEX
        model.train()
        
        loss = 0
        total_samples = 0
        
        if return_embedding:
            embeddings = []
            preds = []
        
        tbar = tqdm(dataloader)
        
        for i, item in enumerate(tbar):
            item = {k: v.to(self.cfg.device) for k, v in item.items()}
            with autocast(enabled = cfg.apex):
                batch_loss, batch_preds, batch_embeddings = model(item['input_ids'], 
                                                                  item['attention_mask'], 
                                                                  label = item['label'])
                batch_size = batch_embeddings.shape[0]
                
            # Backward
            scaler.scale(batch_loss.mean()).backward()
            
            # Update loss
            loss += batch_loss.item() * batch_size
            total_samples += batch_size
            
            tbar.set_description('Batch/Avg Loss: {:.4f}/{:.4f}'.format(batch_loss, loss / total_samples))
            
            # Update the gradient
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()
            scheduler.step()
            
            if return_embedding:
                embeddings.append(batch_embeddings.detach().cpu().numpy())
                preds.append(batch_preds.detach().cpu().numpy())
        
        ckp = os.path.join(self.cfg.model_dir, self.cfg.ver[:-1], self.cfg.ver[-1], f'stg2_model.pt')
        torch.save(model.state_dict(), ckp)
        
        gc.collect()
        torch.cuda.empty_cache()
        
        if return_embedding:
            embeddings = np.concatenate(embeddings, axis = 0)
            preds = np.concatenate(preds, axis = 0)        
            
            return embeddings, preds
        
        return None, None
    
    def train(self, return_embedding = True):
        model, dataloader, optimizer, scheduler = self._prepare_materials()
        
        print_log(self.cfg, 'Start training...')
        for epoch in range(self.cfg.stg2_nepochs):
            embeddings, preds = self._train_epoch(model, dataloader, optimizer, scheduler, return_embedding = return_embedding)
            
        return embeddings, preds
    
    def infer_embedding(self):
        model = self._prepare_model()
        dataloader = self._prepare_dataloader(mode = 'infer')
        ckp = os.path.join(self.cfg.model_dir, self.cfg.ver[:-1], self.cfg.ver[-1], f'stg2_model.pt')
        model.load_state_dict(torch.load(ckp, map_location = self.cfg.device))
        model = model.to(self.cfg.device)
        model.eval()
        
        embeddings = []
        preds = []
        
        if self.cfg.use_tqdm:
            tbar = tqdm(dataloader)
        else:
            tbar = dataloader

        for i, item in enumerate(tbar):
            item = {k: v.to(self.cfg.device) for k, v in item.items()}
            
            with torch.no_grad():
                with autocast(enabled = self.cfg.apex):
                    _, batch_preds, batch_embeddings = model(item['input_ids'], item['attention_mask'])
                        
            embeddings.append(batch_embeddings.cpu().numpy())
            preds.append(batch_preds.cpu().numpy())
            
        embeddings = np.concatenate(embeddings)
        preds = np.concatenate(preds)
        return embeddings, preds

def negative_sampling(data):
    pos_idx = data.loc[data.label == 1.].index.values
    neg_idx = data.loc[data.label == 0.].sample(frac = cfg.negative_sample_ratio, 
                                                random_state = cfg.seed).index.values
    chosen_idx = np.concatenate([pos_idx, neg_idx])
    return data.loc[chosen_idx].sample(frac = 1.)

sampled_data = negative_sampling(data)
SecondStageTextEmbedding(cfg, sampled_data).train(return_embedding = False)

"""# XGBoost"""

import xgboost as xgb

class TabularModel(object):
    def __init__(
        self,
        cfg,
        model_params,
        data,
        features = None,
        ground_truth = None,
        run_validation = True,
        prob_threshold = 0.5
    ):
        self.cfg = cfg
        self.model_params = model_params
        self.data = data
        self.ground_truth = ground_truth
        self.run_validation = run_validation
        self.prob_threshold = prob_threshold
        
        if run_validation:
            assert self.ground_truth is not None, "The ground-truth dataframe needs to be provided to run validation!"
        
        self.features = features
    
    def _negative_sampling(self):
        pos_idx = self.data.loc[self.data.label == 1.].index.values
        neg_idx = self.data.loc[self.data.label == 0.].sample(frac = self.cfg.negative_sample_ratio, 
                                                              random_state = self.cfg.seed).index.values
        chosen_idx = np.concatenate([pos_idx, neg_idx])
        return self.data.loc[chosen_idx].sample(frac = 1.)
        
    def _prepare_data(self, data):
        embeddings, stg2_preds = SecondStageTextEmbedding(self.cfg, data).infer_embedding()
        
        if self.features is not None:
            embeddings = np.hstack([embeddings, stg2_preds.reshape(-1, 1), data[self.features].values])
        else:
            embeddings = np.hstack([embeddings, stg2_preds.reshape(-1, 1)])
            
        ds = xgb.DMatrix(embeddings, label = data['label'].values)

        return ds
    
    def _choose_candidates(self, df):
        preds = df['preds']
        chosen_content = df.loc[df['preds'] > self.prob_threshold, 'content_id'].tolist()
        if chosen_content == []:
            chosen_content = df.sort_values('preds', ascending = False)['content_id'].tolist()[:5]
        return ' '.join(chosen_content)
    
    def _train(self, data):
        print_log(cfg, 'Prepare the training/validation data...')
        d_train = self._prepare_data(data)
        
        print_log(self.cfg, 'Training...')
        model = xgb.train(
            self.model_params,
            dtrain = d_train,
            num_boost_round = 1500,
            verbose_eval = 150
        )
        
        # Store the model to the hard drive
        path = os.path.join(self.cfg.model_dir, self.cfg.ver[:-1], self.cfg.ver[-1], f'xgb_seed_{self.cfg.seed}.pkl')
        print_log(self.cfg, f'Saving the model to {path}...')
        with open(path, 'wb') as f:
            pickle.dump(model, f)
            
    def _valid(self, data):
        d_valid = self._prepare_data(data)
        
        path = os.path.join(self.cfg.model_dir, self.cfg.ver[:-1], self.cfg.ver[-1], f'xgb_seed_{self.cfg.seed}.pkl')
        print_log(self.cfg, f'Loading the model from {path}...')
        with open(path, 'rb') as f:
            model = pickle.load(f)
            
        preds = model.predict(d_valid)
        data['preds'] = preds
        data = data[['topic_id', 'content_id', 'label', 'preds']]
        data = data.groupby('topic_id').apply(self._choose_candidates)
        data = data.to_frame().reset_index()
        data.columns = ['topic_id', 'content_id']
        
        oof = data.merge(self.ground_truth, on = 'topic_id', how = 'left')
        oof.columns = ['topic_id', 'pred_content_ids', 'content_ids']
        
        score = metric_fn(oof['pred_content_ids'], oof['content_ids'])
        print_log(self.cfg, f'Score: {score}')
        return oof
    
    def fit(self):
        print_log(self.cfg, f' Seed {self.cfg.seed} '.center(50, '*'))
        print_log(self.cfg, 'Negative sampling...')
        data = self._negative_sampling()
        print_log(self.cfg, f'Start training - seed {self.cfg.seed}...')
        self._train(data)
        
        if self.ground_truth is not None:
            valid_data = self.data.loc[(self.data.category != 'source') & self.data.has_content]
            _ = self._valid(valid_data)    # NOTICE: THIS IS THE IN-SAMPLE SCORES

"""* Train"""

# Preparing materials
model_params = {
    'objective': 'binary:logistic',
    'tree_method': 'gpu_hist',
    'booster': 'gbtree',
    'random_state': cfg.seed,
    'learning_rate': 0.1,
    'colsample_bytree': 0.9,
    'eta': 0.05,
    'max_depth': 8, 
    'subsample': 0.8,
    'scale_pos_weight': 1,
}

features = ['distance', 'encoded_language_t', 'encoded_language_c']
ground_truth = pd.read_csv(os.path.join(cfg.comp_data_dir, 'correlations.csv'))

for seed in [1, 11, 111, 1111, 11111, 2, 22, 222, 2222, 22222]:
    cfg.seed = seed
    clf = TabularModel(
        cfg,
        model_params,
        data,
        features = features,
        ground_truth = ground_truth,
        run_validation = True,
        prob_threshold = 0.5
    )
    
    clf.fit()