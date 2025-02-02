import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import optim

import copy
import numpy as np
import math

import numpy as np
import os
import shutil
from tqdm import tqdm
from matplotlib import pyplot as plt
from hyptorch.nn import *
from hyptorch import pmath as pm
from datetime import datetime

from eval import segment_bars_with_confidence

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

def exponential_descrease(idx_decoder, p=3):
    return math.exp(-p*idx_decoder)

class AttentionHelper(nn.Module):
    def __init__(self):
        super(AttentionHelper, self).__init__()
        self.softmax = nn.Softmax(dim=-1)


    def scalar_dot_att(self, proj_query, proj_key, proj_val, padding_mask):
        '''
        scalar dot attention.
        :param proj_query: shape of (B, C, L) => (Batch_Size, Feature_Dimension, Length)
        :param proj_key: shape of (B, C, L)
        :param proj_val: shape of (B, C, L)
        :param padding_mask: shape of (B, C, L)
        :return: attention value of shape (B, C, L)
        '''
        m, c1, l1 = proj_query.shape
        m, c2, l2 = proj_key.shape
        
        assert c1 == c2
        
        energy = torch.bmm(proj_query.permute(0, 2, 1), proj_key)  # out of shape (B, L1, L2)
        attention = energy / np.sqrt(c1)
        attention = attention + torch.log(padding_mask + 1e-6) # mask the zero paddings. log(1e-6) for zero paddings
        attention = self.softmax(attention) 
        attention = attention * padding_mask
        attention = attention.permute(0,2,1)
        out = torch.bmm(proj_val, attention)
        return out, attention

class AttLayer(nn.Module):
    def __init__(self, q_dim, k_dim, v_dim, r1, r2, r3, bl, stage, att_type): # r1 = r2
        super(AttLayer, self).__init__()
        
        self.query_conv = nn.Conv1d(in_channels=q_dim, out_channels=q_dim // r1, kernel_size=1)
        self.key_conv = nn.Conv1d(in_channels=k_dim, out_channels=k_dim // r2, kernel_size=1)
        self.value_conv = nn.Conv1d(in_channels=v_dim, out_channels=v_dim // r3, kernel_size=1)
        
        self.conv_out = nn.Conv1d(in_channels=v_dim // r3, out_channels=v_dim, kernel_size=1)

        self.bl = bl
        self.stage = stage
        self.att_type = att_type
        assert self.att_type in ['normal_att', 'block_att', 'sliding_att']
        assert self.stage in ['encoder','decoder']
        
        self.att_helper = AttentionHelper()
        self.window_mask = self.construct_window_mask()
        
    
    def construct_window_mask(self):
        '''
            construct window mask of shape (1, l, l + l//2 + l//2), used for sliding window self attention
        '''
        window_mask = torch.zeros((1, self.bl, self.bl + 2* (self.bl //2)))
        for i in range(self.bl):
            window_mask[:, :, i:i+self.bl] = 1
        return window_mask.to(device)
    
    def forward(self, x1, x2, mask):
        # x1 from the encoder
        # x2 from the decoder
        
        query = self.query_conv(x1)
        key = self.key_conv(x1)
         
        if self.stage == 'decoder':
            assert x2 is not None
            value = self.value_conv(x2)
        else:
            value = self.value_conv(x1)
            
        if self.att_type == 'normal_att':
            return self._normal_self_att(query, key, value, mask)
        elif self.att_type == 'block_att':
            return self._block_wise_self_att(query, key, value, mask)
        elif self.att_type == 'sliding_att':
            return self._sliding_window_self_att(query, key, value, mask)

    
    def _normal_self_att(self,q,k,v, mask):
        m_batchsize, c1, L = q.size()
        _,c2,L = k.size()
        _,c3,L = v.size()
        padding_mask = torch.ones((m_batchsize, 1, L)).to(device) * mask[:,0:1,:]
        output, attentions = self.att_helper.scalar_dot_att(q, k, v, padding_mask)
        output = self.conv_out(F.relu(output))
        output = output[:, :, 0:L]
        return output * mask[:, 0:1, :]  
        
    def _block_wise_self_att(self, q,k,v, mask):
        m_batchsize, c1, L = q.size()
        _,c2,L = k.size()
        _,c3,L = v.size()
        
        nb = L // self.bl
        if L % self.bl != 0:
            q = torch.cat([q, torch.zeros((m_batchsize, c1, self.bl - L % self.bl)).to(device)], dim=-1)
            k = torch.cat([k, torch.zeros((m_batchsize, c2, self.bl - L % self.bl)).to(device)], dim=-1)
            v = torch.cat([v, torch.zeros((m_batchsize, c3, self.bl - L % self.bl)).to(device)], dim=-1)
            nb += 1

        padding_mask = torch.cat([torch.ones((m_batchsize, 1, L)).to(device) * mask[:,0:1,:], torch.zeros((m_batchsize, 1, self.bl * nb - L)).to(device)],dim=-1)

        q = q.reshape(m_batchsize, c1, nb, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize * nb, c1, self.bl)
        padding_mask = padding_mask.reshape(m_batchsize, 1, nb, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize * nb,1, self.bl)
        k = k.reshape(m_batchsize, c2, nb, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize * nb, c2, self.bl)
        v = v.reshape(m_batchsize, c3, nb, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize * nb, c3, self.bl)
        
        output, attentions = self.att_helper.scalar_dot_att(q, k, v, padding_mask)
        output = self.conv_out(F.relu(output))
        
        output = output.reshape(m_batchsize, nb, c3, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize, c3, nb * self.bl)
        output = output[:, :, 0:L]
        return output * mask[:, 0:1, :]  
    
    def _sliding_window_self_att(self, q,k,v, mask):
        m_batchsize, c1, L = q.size()
        _, c2, _ = k.size()
        _, c3, _ = v.size()
        
        
        assert m_batchsize == 1  # currently, we only accept input with batch size 1
        # padding zeros for the last segment
        nb = L // self.bl 
        if L % self.bl != 0:
            q = torch.cat([q, torch.zeros((m_batchsize, c1, self.bl - L % self.bl)).to(device)], dim=-1)
            k = torch.cat([k, torch.zeros((m_batchsize, c2, self.bl - L % self.bl)).to(device)], dim=-1)
            v = torch.cat([v, torch.zeros((m_batchsize, c3, self.bl - L % self.bl)).to(device)], dim=-1)
            nb += 1
        padding_mask = torch.cat([torch.ones((m_batchsize, 1, L)).to(device) * mask[:,0:1,:], torch.zeros((m_batchsize, 1, self.bl * nb - L)).to(device)],dim=-1)
        
        # sliding window approach, by splitting query_proj and key_proj into shape (c1, l) x (c1, 2l)
        # sliding window for query_proj: reshape
        q = q.reshape(m_batchsize, c1, nb, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize * nb, c1, self.bl)
        
        # sliding window approach for key_proj
        # 1. add paddings at the start and end
        k = torch.cat([torch.zeros(m_batchsize, c2, self.bl // 2).to(device), k, torch.zeros(m_batchsize, c2, self.bl // 2).to(device)], dim=-1)
        v = torch.cat([torch.zeros(m_batchsize, c3, self.bl // 2).to(device), v, torch.zeros(m_batchsize, c3, self.bl // 2).to(device)], dim=-1)
        padding_mask = torch.cat([torch.zeros(m_batchsize, 1, self.bl // 2).to(device), padding_mask, torch.zeros(m_batchsize, 1, self.bl // 2).to(device)], dim=-1)
        
        # 2. reshape key_proj of shape (m_batchsize*nb, c1, 2*self.bl)
        k = torch.cat([k[:,:, i*self.bl:(i+1)*self.bl+(self.bl//2)*2] for i in range(nb)], dim=0) # special case when self.bl = 1
        v = torch.cat([v[:,:, i*self.bl:(i+1)*self.bl+(self.bl//2)*2] for i in range(nb)], dim=0) 
        # 3. construct window mask of shape (1, l, 2l), and use it to generate final mask
        padding_mask = torch.cat([padding_mask[:,:, i*self.bl:(i+1)*self.bl+(self.bl//2)*2] for i in range(nb)], dim=0) # of shape (m*nb, 1, 2l)
        final_mask = self.window_mask.repeat(m_batchsize * nb, 1, 1) * padding_mask 
        
        output, attention = self.att_helper.scalar_dot_att(q, k, v, final_mask)
        output = self.conv_out(F.relu(output))

        output = output.reshape(m_batchsize, nb, -1, self.bl).permute(0, 2, 1, 3).reshape(m_batchsize, -1, nb * self.bl)
        output = output[:, :, 0:L]
        return output * mask[:, 0:1, :]


class MultiHeadAttLayer(nn.Module):
    def __init__(self, q_dim, k_dim, v_dim, r1, r2, r3, bl, stage, att_type, num_head):
        super(MultiHeadAttLayer, self).__init__()
#         assert v_dim % num_head == 0
        self.conv_out = nn.Conv1d(v_dim * num_head, v_dim, 1)
        self.layers = nn.ModuleList(
            [copy.deepcopy(AttLayer(q_dim, k_dim, v_dim, r1, r2, r3, bl, stage, att_type)) for i in range(num_head)])
        self.dropout = nn.Dropout(p=0.5)
        
    def forward(self, x1, x2, mask):
        out = torch.cat([layer(x1, x2, mask) for layer in self.layers], dim=1)
        out = self.conv_out(self.dropout(out))
        return out
            

class ConvFeedForward(nn.Module):
    def __init__(self, dilation, in_channels, out_channels):
        super(ConvFeedForward, self).__init__()
        self.layer = nn.Sequential(
            nn.Conv1d(in_channels, out_channels, 3, padding=dilation, dilation=dilation),
            nn.ReLU()
        )

    def forward(self, x):
        return self.layer(x)


class FCFeedForward(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(FCFeedForward, self).__init__()
        self.layer = nn.Sequential(
            nn.Conv1d(in_channels, out_channels, 1),  # conv1d equals fc
            nn.ReLU(),
            nn.Dropout(),
            nn.Conv1d(out_channels, out_channels, 1)
        )
        
    def forward(self, x):
        return self.layer(x)
    

class AttModule(nn.Module):
    def __init__(self, dilation, in_channels, out_channels, r1, r2, att_type, stage, alpha):
        super(AttModule, self).__init__()
        self.feed_forward = ConvFeedForward(dilation, in_channels, out_channels)
        self.instance_norm = nn.InstanceNorm1d(in_channels, track_running_stats=False)
        self.att_layer = AttLayer(in_channels, in_channels, out_channels, r1, r1, r2, dilation, att_type=att_type, stage=stage) # dilation
        self.conv_1x1 = nn.Conv1d(out_channels, out_channels, 1)
        self.dropout = nn.Dropout()
        self.alpha = alpha
        
    def forward(self, x, f, mask):
        out = self.feed_forward(x)
        out = self.alpha * self.att_layer(self.instance_norm(out), f, mask) + out
        out = self.conv_1x1(out)
        out = self.dropout(out)
        return (x + out) * mask[:, 0:1, :]


class PositionalEncoding(nn.Module):
    "Implement the PE function."

    def __init__(self, d_model, max_len=10000):
        super(PositionalEncoding, self).__init__()
        # Compute the positional encodings once in log space.
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) *
                             -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).permute(0,2,1) # of shape (1, d_model, l)
        self.pe = nn.Parameter(pe, requires_grad=True)
#         self.register_buffer('pe', pe)

    def forward(self, x):
        return x + self.pe[:, :, 0:x.shape[2]]

class Encoder(nn.Module):
    def __init__(self, num_layers, r1, r2, num_f_maps, input_dim, num_classes, channel_masking_rate, att_type, alpha):
        super(Encoder, self).__init__()
        self.conv_1x1 = nn.Conv1d(input_dim, num_f_maps, 1) # fc layer
        self.layers = nn.ModuleList(
            [AttModule(2 ** i, num_f_maps, num_f_maps, r1, r2, att_type, 'encoder', alpha) for i in # 2**i
             range(num_layers)])
        
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)
        self.dropout = nn.Dropout2d(p=channel_masking_rate)
        self.channel_masking_rate = channel_masking_rate

    def forward(self, x, mask):
        '''
        :param x: (N, C, L)
        :param mask:
        :return:
        '''

        if self.channel_masking_rate > 0:
            x = x.unsqueeze(2)
            x = self.dropout(x)
            x = x.squeeze(2)

        feature = self.conv_1x1(x)
        for layer in self.layers:
            feature = layer(feature, None, mask)
        
        out = self.conv_out(feature) * mask[:, 0:1, :]

        return out, feature


class Decoder(nn.Module):
    def __init__(self, num_layers, r1, r2, num_f_maps, input_dim, num_classes, att_type, alpha):
        super(Decoder, self).__init__()#         self.position_en = PositionalEncoding(d_model=num_f_maps)
        self.conv_1x1 = nn.Conv1d(input_dim, num_f_maps, 1)
        self.layers = nn.ModuleList(
            [AttModule(2 ** i, num_f_maps, num_f_maps, r1, r2, att_type, 'decoder', alpha) for i in # 2 ** i
             range(num_layers)])
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x, fencoder, mask):

        feature = self.conv_1x1(x)
        for layer in self.layers:
            feature = layer(feature, fencoder, mask)

        out = self.conv_out(feature) * mask[:, 0:1, :]

        return out, feature
    
class MyTransformer(nn.Module):
    def __init__(self, num_decoders, num_layers, r1, r2, num_f_maps, input_dim, output_dim, num_classes, channel_masking_rate):
        super(MyTransformer, self).__init__()
        self.encoder = Encoder(num_layers, r1, r2, num_f_maps, input_dim, num_classes, channel_masking_rate, att_type='sliding_att', alpha=1)
        self.decoders = nn.ModuleList([copy.deepcopy(Decoder(num_layers, r1, r2, num_f_maps, num_classes, num_classes, att_type='sliding_att', alpha=exponential_descrease(s))) for s in range(num_decoders)]) # num_decoders
        self.hypmlp = HypMlp(num_f_maps, output_dim)

        self.loss1 = BinaryTreeLoss()
        self.loss2 = NormLoss()
        self.loss_crossen = CrossEn()
        
    def forward(self, x, mask):
        out, feature = self.encoder(x, mask)
        outputs = out.unsqueeze(0)
        
        for decoder in self.decoders:
            out, feature = decoder(F.softmax(out, dim=1) * mask[:, 0:1, :], feature* mask[:, 0:1, :], mask)
            outputs = torch.cat((outputs, out.unsqueeze(0)), dim=0)
        
        # in the hyperbolic space
        outputs = self.hypmlp(feature.transpose(2, 1)[0])
 
        return outputs
    
    '''def loss(self, parent, child, bz=4):
        loss = []
        for n in range(0, len(parent), bz):
            parent_batch = parent[bz*n: bz*(n+1)]
            child_batch = child[bz*n: bz*(n+1)]
            # idx = np.random.choice(len(parent), bz)
            # parent_batch = parent[idx]
            # child_batch = child[idx]
            batch_loss = self.loss1(parent_batch, child_batch) + self.loss2(parent_batch, child_batch)
            # print('loss_binary', self.loss1(parent_batch, child_batch))
            # print('loss_norm', self.loss2(parent_batch, child_batch))
            if torch.isnan(batch_loss):
                print('batch %d loss' % n, batch_loss)
                break
            loss.append(batch_loss)
        return sum(loss) / len(loss)
        # return self.loss1(parent[:bz], child[:bz]) + self.loss2(parent[:bz], child[:bz])'''

    def loss(self, parent, child, features, target, bz=4):
        loss_cos = []
        loss_center = 0.
        loss_norm = []
        # print(parent.shape, child.shape, features.shape, target.shape)
        # e.g. torch.Size([5537, 64]) torch.Size([5537, 64]) torch.Size([5558, 64]) torch.Size([5558])
        
        # cos
        boarder = torch.argwhere(target != torch.cat([target[[-1]], target[:-1]]))
        boarder = boarder.squeeze().cpu().tolist() + [len(target)]
        for n in range(len(target) // (len(boarder)-1) + 1):
            cross_idx_1 = []
            cross_idx_2 = []
            for n in range(len(boarder)-1):
                cross_idx_1.append(np.random.choice(range(boarder[n], boarder[n+1])))
                cross_idx_2.append(np.random.choice(range(boarder[n], boarder[n+1])))
            score = torch.matmul(features[cross_idx_1], features[cross_idx_2].T)
            batch_loss = self.loss_crossen(score)
            loss_cos.append(batch_loss)
        
        # center
        start_point_features_norm = features[boarder[:-1]].norm(dim=1)
        loss_center = - start_point_features_norm.mean()

        # norm
        # for n in range(0, len(parent), bz):
        #     parent_batch_norm = parent[bz*n: bz*(n+1)].norm(dim=1)
        #     child_batch_norm = child[bz*n: bz*(n+1)].norm(dim=1)
            # idx = np.random.choice(len(parent), bz)
            # parent_batch_norm = parent[idx].norm(dim=1)
            # child_batch_norm = child[idx].norm(dim=1)

            # print(parent_batch_norm.shape, child_batch_norm.shape)
            # torch.Size([4]) torch.Size([4])
        for n in range(len(boarder)-1):
            parent_batch_norm = parent[boarder[n]: boarder[n+1]].norm(dim=1)
            child_batch_norm = child[boarder[n]: boarder[n+1]].norm(dim=1)
            # print(len(parent_batch_norm))
            score = - F.relu(child_batch_norm.repeat(len(child_batch_norm), 1) - parent_batch_norm.repeat(len(child_batch_norm), 1).T + 0.01)
            batch_loss = self.loss_crossen(score)
            if torch.isnan(batch_loss):
                print('batch %d loss' % n, batch_loss)
                break
            loss_norm.append(batch_loss)

        # print((sum(loss_cos) / len(loss_cos)), loss_center, (sum(loss_norm) / len(loss_norm)))

        return (sum(loss_cos) / len(loss_cos)) + loss_center + (sum(loss_norm) / len(loss_norm))


class HypMlp(nn.Module):
    def __init__(self, input_dim, output_dim, act_layer=nn.GELU, drop=0.05):
        super(HypMlp, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.topoincare = ToPoincare(c=1)
        self.hypfc1 = HypLinear(input_dim, input_dim, c=1)
        self.hypfc2 = HypLinear(input_dim, input_dim, c=1)
        self.hypfc3 = HypLinear(input_dim, output_dim, c=1)
        self.act = act_layer()
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.topoincare(x)
        x = self.hypfc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.hypfc2(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.hypfc3(x)
        return x


class CrossEn(nn.Module):
    def __init__(self,):
        super(CrossEn, self).__init__()

    def forward(self, sim_matrix):
        logpt = F.log_softmax(sim_matrix, dim=-1)
        logpt = torch.diag(logpt)
        nce_loss = -logpt
        sim_loss = nce_loss.mean()
        return sim_loss
    

class BinaryTreeLoss(nn.Module):
    def __init__(self, c=1):
        super(BinaryTreeLoss, self).__init__()
        self.c = c

    def forward(self, parent, child):
        dis_matrix = pm.dist_matrix(child, parent, self.c)
        logpt = F.log_softmax(-dis_matrix, dim=-1)
        logpt = torch.diag(logpt)
        nce_loss = -logpt
        loss = nce_loss.mean()
        return loss


class NormLoss(nn.Module):
    def __init__(self, c=1, alpha=1):
        super(NormLoss, self).__init__()
        self.c = c
        self.alpha = alpha

    def forward(self, parent, child):
        child_norm = child.norm(dim=1)
        parent_norm = parent.norm(dim=1)
        score = - self.alpha * F.relu(child_norm - parent_norm + 0.01)
        # score = - self.alpha * pm.dist_matrix(child, parent, self.c) * F.relu(child_norm - parent_norm + 0.01)
        loss = score.mean()
        return loss

    
class Trainer:
    def __init__(self, num_layers, r1, r2, num_f_maps, input_dim, output_dim, num_classes, channel_masking_rate):
        self.model = MyTransformer(3, num_layers, r1, r2, num_f_maps, input_dim, output_dim, num_classes, channel_masking_rate)
        # self.model = HypMlp(input_dim, 2)
        self.ce = nn.CrossEntropyLoss(ignore_index=-100)

        print('Model Size: ', sum(p.numel() for p in self.model.parameters()))
        self.mse = nn.MSELoss(reduction='none')
        self.num_classes = num_classes
        
        # visualize dir
        self.dir = './visualize/breakfast_base64-16_StartCloseToCenter+SeparateSegments+Shuffle'
        if os.path.exists('./' + self.dir):
            shutil.rmtree('./' + self.dir)
            os.makedirs('./' + self.dir)
        else:
            os.makedirs('./' + self.dir)
        with open('./' + self.dir + '/log.txt', mode='w') as f:
            f.write(str(datetime.now()) + '\n')

    def train(self, save_dir, batch_gen, num_epochs, batch_size, learning_rate, batch_gen_tst=None):
        self.model.train()
        self.model.to(device)
        # self.model.load_state_dict(torch.load('/storage/rqshi/ASFormer/models_original/50salads/split_5/epoch-120.model'), strict=False)
        optimizer = optim.Adam(self.model.parameters(), lr=learning_rate, weight_decay=1e-5)
        print('LR:{}'.format(learning_rate))
        
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=3, verbose=True)
        for epoch in range(num_epochs):
            epoch_loss = 0
            # correct = 0
            # total = 0
            cnt = 0

            # while batch_gen.has_next():
            for _ in tqdm(range(len(batch_gen))):
            # for _ in tqdm(range(10)):
                batch_input, batch_target, mask, vids = batch_gen.next_batch(batch_size, False)
                batch_input, batch_target, mask = batch_input.to(device), batch_target.to(device), mask.to(device)
                optimizer.zero_grad()
                fs = self.model(batch_input, mask)
                # print(fs.shape, batch_target.T.shape)

                target = batch_target.T
                ps_idx = (target == torch.cat((target[1:], target[[0]]))).squeeze()
                cs_idx = (target == torch.cat((target[[-1]], target[:-1]))).squeeze()
                
                # for p, c, t in zip(ps_idx, cs_idx, target):
                #     print(p, c, t)
                # print(ps_idx.shape, cs_idx.shape, target.shapes)
                # torch.Size([5558]) torch.Size([5558]) torch.Size([5558, 1])

                ps = fs[ps_idx]
                cs = fs[cs_idx]
                # print(ps.shape, cs.shape, ps_target.shape)
                # torch.Size([5537, 64]) torch.Size([5537, 64]) torch.Size([5537])
                
                # plot
                # if cnt < 7:
                # if sum([(n in vids[0]) for n in ['04-1', '05-1', '11-2', '13-2', '17-2', '20-1', '21-1']]):
                #     self._plot(epoch, vids[0], fs, target, dir=self.dir)
                self._plot(epoch, vids[0], fs, target, dir=self.dir)
                cnt += 1

                # print(ps.shape, cs.shape)
                loss = self.model.loss(ps, cs, fs, target.squeeze())
                # print('loss', loss)

                epoch_loss += loss.item()
                loss.backward()
                optimizer.step()

                # _, predicted = torch.max(ps.data[-1], 1)
                # correct += ((predicted == batch_target).float() * mask[:, 0, :].squeeze(1)).sum().item()
                # total += torch.sum(mask[:, 0, :]).item()
            
            
            scheduler.step(epoch_loss)
            batch_gen.reset()
            print("[epoch %d]: epoch loss = %f" % (epoch + 1, epoch_loss / len(batch_gen.list_of_examples)))
            with open('./' + self.dir + '/log.txt', mode='a') as f:
                f.write("[epoch %d]: epoch loss = %f\n" % (epoch + 1, epoch_loss / len(batch_gen.list_of_examples)))

            if (epoch + 1) % 10 == 0 and batch_gen_tst is not None:
                # self.test(batch_gen_tst, epoch)
                torch.save(self.model.state_dict(), save_dir + "/epoch-" + str(epoch + 1) + ".model")
                torch.save(optimizer.state_dict(), save_dir + "/epoch-" + str(epoch + 1) + ".opt")

    def test(self, batch_gen_tst, epoch):
        self.model.eval()
        correct = 0
        total = 0
        if_warp = False  # When testing, always false
        with torch.no_grad():
            while batch_gen_tst.has_next():
                batch_input, batch_target, mask, vids = batch_gen_tst.next_batch(1, if_warp)
                batch_input, batch_target, mask = batch_input.to(device), batch_target.to(device), mask.to(device)
                p = self.model(batch_input, mask)
                _, predicted = torch.max(p.data[-1], 1)
                correct += ((predicted == batch_target).float() * mask[:, 0, :].squeeze(1)).sum().item()
                total += torch.sum(mask[:, 0, :]).item()

        acc = float(correct) / total
        print("---[epoch %d]---: tst acc = %f" % (epoch + 1, acc))

        self.model.train()
        batch_gen_tst.reset()

    def predict(self, model_dir, results_dir, features_path, batch_gen_tst, epoch, actions_dict, sample_rate):
        self.model.eval()
        with torch.no_grad():
            self.model.to(device)
            self.model.load_state_dict(torch.load(model_dir + "/epoch-" + str(epoch) + ".model"))

            batch_gen_tst.reset()
            import time
            
            time_start = time.time()
            while batch_gen_tst.has_next():
                batch_input, batch_target, mask, vids = batch_gen_tst.next_batch(1)
                vid = vids[0]
#                 print(vid)
                features = np.load(features_path + vid.split('.')[0] + '.npy')
                features = features[:, ::sample_rate]

                input_x = torch.tensor(features, dtype=torch.float)
                input_x.unsqueeze_(0)
                input_x = input_x.to(device)
                predictions = self.model(input_x, torch.ones(input_x.size(), device=device))

                for i in range(len(predictions)):
                    confidence, predicted = torch.max(F.softmax(predictions[i], dim=1).data, 1)
                    confidence, predicted = confidence.squeeze(), predicted.squeeze()
 
                    batch_target = batch_target.squeeze()
                    confidence, predicted = confidence.squeeze(), predicted.squeeze()
 
                    segment_bars_with_confidence(results_dir + '/{}_stage{}.png'.format(vid, i),
                                                 confidence.tolist(),
                                                 batch_target.tolist(), predicted.tolist())

                recognition = []
                for i in range(len(predicted)):
                    recognition = np.concatenate((recognition, [list(actions_dict.keys())[
                                                                    list(actions_dict.values()).index(
                                                                        predicted[i].item())]] * sample_rate))
                f_name = vid.split('/')[-1].split('.')[0]
                f_ptr = open(results_dir + "/" + f_name, "w")
                f_ptr.write("### Frame level recognition: ###\n")
                f_ptr.write(' '.join(recognition))
                f_ptr.close()
            time_end = time.time()
    
    def _plot(self, epoch, vid, features, target, clip_num=16, dir='visualize_base64-16'):
        if epoch % 3 != 0:
            return
        vid = vid.split('.')[0]
        segment_features = []
        last_n = 0
        for n in range(len(target[:-1])):
            if target[n] != target[n+1]:
                segment_features.append(features[last_n: n+1])
                last_n = n + 1

        colors = ['b', 'c', 'g', 'm', 'r', 'y', 'orange', 'darkgray']
        # r = float(features.norm(dim=1).max() * 1.2)
        r = 1
        
        max_length = 0
        text_x = []
        text_y = []
        text_x_end = []
        text_y_end = []
        feature_norm_all = []
        # single video features
        for t, feature in enumerate(segment_features):
            clip_num = 16
            feature = feature[::max(1, len(feature)//clip_num)].cpu().detach()
            feature_norm = feature.norm(dim=1)
            text_x.append(max_length)
            text_y.append(feature_norm[0])
            max_length += len(feature_norm)
            text_x_end.append(max_length-1)
            text_y_end.append(feature_norm[-1])
            feature_norm_all += [float(norm) for norm in feature_norm]

            '''plt.figure(figsize=(18, 18))
            # canvas
            plt.title('Epoch %d: %s samples' % (epoch, vid))
            plt.xlim(-r, r)
            plt.ylim(-r, r)

            # circle
            circle = plt.Circle((0, 0), r, color="black", fill=False)
            plt.gcf().gca().add_artist(circle)
            plt.scatter(0, 0, color='black', marker = 'x')

            for n in range(len(feature)-1):
                plt.scatter(feature[n][0], feature[n][1], color=colors[t%len(colors)], marker='o')
                plt.plot([feature[n][0], feature[n+1][0]], [feature[n][1], feature[n+1][1]], color='black')
            plt.scatter(feature[-1][0], feature[-1][1], color=colors[t%len(colors)], marker='o')
            # plt.text(feature[0][0], feature[0][1], vid)

            plt.savefig(dir + '/%s_%d_epoch%d.png' % (vid, t, epoch))
            plt.close()'''

            # # norm
            # plt.figure(figsize=(18, 4))
            # plt.title('norm')
            # plt.plot(feature_norm)
            # plt.savefig(dir + '/%s_norm%d_epoch%d.png' % (vid, t, epoch))
            # plt.close()
        
        # norm all
        plt.figure(figsize=(64, 4))
        plt.title('Norm of all segments')
        plt.plot(feature_norm_all)
        for (x, y, norm) in zip(text_x, text_y, range(len(feature_norm_all))):
            plt.text(x, y, norm+1, fontsize=20)
        for (x, y, norm) in zip(text_x_end, text_y_end, range(len(feature_norm_all))):
            plt.text(x-1, y, norm+1, fontsize=20, color='red')
            # plt.text(x, y, round(norm, 3))
        plt.grid()
        print(dir + '/%s_norm_epoch%d.png' % (vid, epoch))
        plt.savefig(dir + '/%s_norm_epoch%d.png' % (vid, epoch))
        plt.close()
        
        '''# whole
        # canvas
        plt.figure(figsize=(18, 18))
        plt.title('Epoch %d: %s samples' % (epoch, vid))
        plt.xlim(-r, r)
        plt.ylim(-r, r)

        # circle
        circle = plt.Circle((0, 0), r, color="black", fill=False)
        plt.gcf().gca().add_artist(circle)
        plt.scatter(0, 0, color='black', marker = 'x')

        for t, feature in enumerate(segment_features):
            feature = feature[::len(feature)//clip_num].cpu().detach()
            for n in range(len(feature)-1):
                plt.scatter(feature[n][0], feature[n][1], color=colors[t%len(colors)], marker='o')
                plt.plot([feature[n][0], feature[n+1][0]], [feature[n][1], feature[n+1][1]], color='black')
            plt.scatter(feature[-1][0], feature[-1][1], color=colors[t%len(colors)], marker='o')
            plt.text(feature[0][0], feature[0][1], str(t))
        plt.savefig(dir + '/%s_whole_epoch%d.png' % (vid, epoch))
        plt.close()'''

            
            

if __name__ == '__main__':
    pass
