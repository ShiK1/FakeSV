import copy
import json
import os
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import tqdm
from sklearn.metrics import *
from tqdm import tqdm
from transformers import AutoConfig, BertModel
from transformers.models.bert.modeling_bert import BertLayer
from zmq import device

from .coattention import *
from .layers import *
from utils.metrics import *
from models.torchvggish import vggish
from transformers import AutoTokenizer, BertModel

class SVFENDModel(torch.nn.Module):
    def __init__(self,bert_model=None,fea_dim=128,dropout=0.1):
        super(SVFENDModel, self).__init__()

        # self.bert = BertModel.from_pretrained("/home/chen/wzh/FakeSV/code/bert-base-chinese", local_files_only= True).requires_grad_(False)
        self.bert = BertModel.from_pretrained("/home/chen/wzh/FakeSV/code/bert-base-chinese").requires_grad_(False)
        
        self.text_dim = 768
        self.comment_dim = 768
        self.img_dim = 4096
        self.video_dim = 4096
        self.num_frames = 83
        self.num_audioframes = 50
        self.num_comments = 23
        self.dim = fea_dim
        self.num_heads = 4

        self.dropout = dropout

        self.attention = Attention(dim=self.dim,heads=4,dropout=dropout)

        vgg1 = vggish()
        self.vggish_modified = vgg1.embeddings

        self.co_attention_ta = co_attention(d_k=fea_dim, d_v=fea_dim, n_heads=self.num_heads, dropout=self.dropout, d_model=fea_dim,
                                        visual_len=self.num_audioframes, sen_len=512, fea_v=self.dim, fea_s=self.dim, pos=False)
        self.co_attention_tv = co_attention(d_k=fea_dim, d_v=fea_dim, n_heads=self.num_heads, dropout=self.dropout, d_model=fea_dim,
                                        visual_len=self.num_frames, sen_len=512, fea_v=self.dim, fea_s=self.dim, pos=False)
        self.trm = nn.TransformerEncoderLayer(d_model = self.dim, nhead = 2, batch_first = True)


        self.linear_text = nn.Sequential(torch.nn.Linear(self.text_dim, fea_dim), torch.nn.ReLU(),nn.Dropout(p=self.dropout))
        self.linear_comment = nn.Sequential(torch.nn.Linear(self.comment_dim, fea_dim), torch.nn.ReLU(),nn.Dropout(p=self.dropout))
        self.linear_img = nn.Sequential(torch.nn.Linear(self.img_dim, fea_dim), torch.nn.ReLU(),nn.Dropout(p=self.dropout))
        self.linear_video = nn.Sequential(torch.nn.Linear(self.video_dim, fea_dim), torch.nn.ReLU(),nn.Dropout(p=self.dropout))
        self.linear_intro = nn.Sequential(torch.nn.Linear(self.text_dim, fea_dim),torch.nn.ReLU(),nn.Dropout(p=self.dropout))
        self.linear_audio = nn.Sequential(torch.nn.Linear(fea_dim, fea_dim), torch.nn.ReLU(),nn.Dropout(p=self.dropout))

        self.classifier = nn.Linear(fea_dim,2)
    
    def forward(self,  **kwargs):

        ### Title ###
        title_inputid = kwargs['title_inputid']#(batch,512)
        title_mask=kwargs['title_mask']#(batch,512)

        fea_text=self.bert(title_inputid,attention_mask=title_mask)['last_hidden_state']#(batch,sequence,768)
        fea_text=self.linear_text(fea_text) 

        ### Audio Frames ###
        audioframes=kwargs['audioframes']#(batch,36,12288)
        audioframes_masks = kwargs['audioframes_masks']
        fea_audio = self.vggish_modified(audioframes) #(batch, frames, 128)
        fea_audio = self.linear_audio(fea_audio) 
        fea_audio, fea_text = self.co_attention_ta(v=fea_audio, s=fea_text, v_len=fea_audio.shape[1], s_len=fea_text.shape[1])
        fea_audio = torch.mean(fea_audio, -2)

        ### Image Frames ###
        frames=kwargs['frames']#(batch,30,4096)
        frames_masks = kwargs['frames_masks']
        fea_img = self.linear_img(frames) 
        fea_img, fea_text = self.co_attention_tv(v=fea_img, s=fea_text, v_len=fea_img.shape[1], s_len=fea_text.shape[1])
        fea_img = torch.mean(fea_img, -2)

        fea_text = torch.mean(fea_text, -2)

        ### C3D ###
        c3d = kwargs['c3d'] # (batch, 36, 4096)
        c3d_masks = kwargs['c3d_masks']
        fea_video = self.linear_video(c3d) #(batch, frames, 128)
        fea_video = torch.mean(fea_video, -2)

        ### Comment ###
        comments_inputid = kwargs['comments_inputid']#(batch,20,250)
        comments_mask=kwargs['comments_mask']#(batch,20,250)
        bert_fea=self.bert(comments_inputid, attention_mask=comments_mask)['last_hidden_state']
        fea_comments = bert_fea
        fea_comments = self.linear_comment(fea_comments[:,-1,:])

        fea_text = fea_text.unsqueeze(1)
        fea_comments = fea_comments.unsqueeze(1)
        fea_img = fea_img.unsqueeze(1)
        fea_audio = fea_audio.unsqueeze(1)
        fea_video = fea_video.unsqueeze(1)

        fea=torch.cat((fea_text,fea_audio, fea_comments, fea_video,fea_img),1) # (bs, 6, 128)
        fea = self.trm(fea)
        fea = torch.mean(fea, -2)
        
        output = self.classifier(fea)

        return output, fea
