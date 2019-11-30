"""
.. module:: lm_lstm_crf
    :synopsis: lm_lstm_crf
 
.. moduleauthor:: Liyuan Liu
"""

import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.optim as optim
import numpy as np
import model.crf as crf
import model.utils as utils
import model.highway as highway
import model.submodel as submodel
import sys

class LM_LSTM_CRF(nn.Module):
    """LM_LSTM_CRF model

    args: 
        tagset_size: size of label set
        char_size: size of char dictionary
        char_dim: size of char embedding
        char_hidden_dim: size of char-level lstm hidden dim
        char_rnn_layers: number of char-level lstm layers
        embedding_dim: size of word embedding
        word_hidden_dim: size of word-level blstm hidden dim
        word_rnn_layers: number of word-level lstm layers
        vocab_size: size of word dictionary
        dropout_ratio: dropout ratio
        large_CRF: use CRF_L or not, refer model.crf.CRF_L and model.crf.CRF_S for more details
        if_highway: use highway layers or not
        in_doc_words: number of words that occurred in the corpus (used for language model prediction)
        highway_layers: number of highway layers
    """
    
    def __init__(
        self, 
        tagset_size, char_size, char_dim, char_hidden_dim, 
        char_rnn_layers, embedding_dim, word_hidden_dim, 
        word_rnn_layers, vocab_size, dropout_ratio, file_num, 
        len_max_seq, n_layers=1, n_head=8, d_k=64, d_v=64, 
        large_CRF=True, if_highway = False, 
        in_doc_words = 2, highway_layers = 1, word_level_attention = False):

        super(LM_LSTM_CRF, self).__init__()
        self.char_dim = char_dim
        self.char_hidden_dim = char_hidden_dim
        self.char_size = char_size
        self.word_dim = embedding_dim
        self.word_hidden_dim = word_hidden_dim
        self.word_size = vocab_size
        self.if_highway = if_highway
        self.word_level_attention = word_level_attention

        self.char_embeds = nn.Embedding(char_size, char_dim)
        self.forw_char_lstm = nn.LSTM(char_dim, char_hidden_dim, num_layers=char_rnn_layers, bidirectional=False, dropout=dropout_ratio)
        self.back_char_lstm = nn.LSTM(char_dim, char_hidden_dim, num_layers=char_rnn_layers, bidirectional=False, dropout=dropout_ratio)
        self.char_rnn_layers = char_rnn_layers

        self.word_embeds = nn.Embedding(vocab_size, embedding_dim)

        self.word_lstm = nn.LSTM(embedding_dim + char_hidden_dim * 2, word_hidden_dim // 2, num_layers=word_rnn_layers, bidirectional=True, dropout=dropout_ratio)

        self.word_rnn_layers = word_rnn_layers

        self.dropout = nn.Dropout(p=dropout_ratio)

        self.tagset_size = tagset_size
        self.crflist = nn.ModuleList()
        for i in range(file_num):
            if large_CRF:
                self.crflist.append(crf.CRF_L(word_hidden_dim, tagset_size))
            else:
                self.crflist.append(crf.CRF_S(word_hidden_dim, tagset_size))

        if if_highway:
            self.forw2char = highway.hw(char_hidden_dim, num_layers=highway_layers, dropout_ratio=dropout_ratio)
            self.back2char = highway.hw(char_hidden_dim, num_layers=highway_layers, dropout_ratio=dropout_ratio)
            self.forw2word = highway.hw(char_hidden_dim, num_layers=highway_layers, dropout_ratio=dropout_ratio)
            self.back2word = highway.hw(char_hidden_dim, num_layers=highway_layers, dropout_ratio=dropout_ratio)
            self.fb2char = highway.hw(2 * char_hidden_dim, num_layers=highway_layers, dropout_ratio=dropout_ratio)

        self.char_pre_train_out = nn.Linear(char_hidden_dim, char_size)
        self.word_pre_train_out = nn.Linear(char_hidden_dim, in_doc_words)

        self.batch_size = 1
        self.word_seq_length = 1

        n_position = len_max_seq + 1
        self.position_enc = nn.Embedding.from_pretrained(
                    utils.get_sinusoid_encoding_table(n_position, embedding_dim, padding_idx=0),
                    freeze=True)

        self.layer_stack = nn.ModuleList([
            submodel.EncoderLayer(embedding_dim*2+char_hidden_dim*2, word_hidden_dim, n_head, d_k, d_v, dropout=dropout_ratio)
            for _ in range(n_layers)])

        self.fc = nn.Linear(embedding_dim*2+char_hidden_dim*2, word_hidden_dim)

    def set_batch_size(self, bsize):
        """
        set batch size
        """
        self.batch_size = bsize

    def set_batch_seq_size(self, sentence):
        """
        set batch size and sequence length
        """
        tmp = sentence.size()
        self.word_seq_length = tmp[0]
        self.batch_size = tmp[1]

    def rand_init_embedding(self):
        """
        random initialize char-level embedding
        """
        utils.init_embedding(self.char_embeds.weight)

    def load_pretrained_word_embedding(self, pre_word_embeddings):
        """
        load pre-trained word embedding

        args:
            pre_word_embeddings (self.word_size, self.word_dim) : pre-trained embedding
        """
        assert (pre_word_embeddings.size()[1] == self.word_dim)
        self.word_embeds.weight = nn.Parameter(pre_word_embeddings)

    def rand_init(self, init_char_embedding=True, init_word_embedding=False):
        """
        random initialization

        args:
            init_char_embedding: random initialize char embedding or not
            init_word_embedding: random initialize word embedding or not
        """
        
        if init_char_embedding:
            utils.init_embedding(self.char_embeds.weight)
        if init_word_embedding:
            utils.init_embedding(self.word_embeds.weight)
        if self.if_highway:
            self.forw2char.rand_init()
            self.back2char.rand_init()
            self.forw2word.rand_init()
            self.back2word.rand_init()
            self.fb2char.rand_init()
        utils.init_lstm(self.forw_char_lstm)
        utils.init_lstm(self.back_char_lstm)
        utils.init_lstm(self.word_lstm)
        utils.init_linear(self.char_pre_train_out)
        utils.init_linear(self.word_pre_train_out)
        utils.init_linear(self.fc)
        for crf in self.crflist:
            crf.rand_init()

    def word_pre_train_forward(self, sentence, position, hidden=None):
        """
        output of forward language model

        args:
            sentence (char_seq_len, batch_size): char-level representation of sentence
            position (word_seq_len, batch_size): position of blank space in char-level representation of sentence
            hidden: initial hidden state

        return:
            language model output (word_seq_len, in_doc_word), hidden
        """
        
        embeds = self.char_embeds(sentence)
        d_embeds = self.dropout(embeds)
        lstm_out, hidden = self.forw_char_lstm(d_embeds)

        tmpsize = position.size()
        position = position.unsqueeze(2).expand(tmpsize[0], tmpsize[1], self.char_hidden_dim)
        select_lstm_out = torch.gather(lstm_out, 0, position)
        d_lstm_out = self.dropout(select_lstm_out).view(-1, self.char_hidden_dim)

        if self.if_highway:
            char_out = self.forw2word(d_lstm_out)
            d_char_out = self.dropout(char_out)
        else:
            d_char_out = d_lstm_out

        pre_score = self.word_pre_train_out(d_char_out)
        return pre_score, hidden

    def word_pre_train_backward(self, sentence, position, hidden=None):
        """
        output of backward language model

        args:
            sentence (char_seq_len, batch_size): char-level representation of sentence (inverse order)
            position (word_seq_len, batch_size): position of blank space in inversed char-level representation of sentence
            hidden: initial hidden state

        return:
            language model output (word_seq_len, in_doc_word), hidden
        """
        embeds = self.char_embeds(sentence)
        d_embeds = self.dropout(embeds)
        lstm_out, hidden = self.back_char_lstm(d_embeds)
        
        tmpsize = position.size()
        position = position.unsqueeze(2).expand(tmpsize[0], tmpsize[1], self.char_hidden_dim)
        select_lstm_out = torch.gather(lstm_out, 0, position)
        d_lstm_out = self.dropout(select_lstm_out).view(-1, self.char_hidden_dim)

        if self.if_highway:
            char_out = self.back2word(d_lstm_out)
            d_char_out = self.dropout(char_out)
        else:
            d_char_out = d_lstm_out

        pre_score = self.word_pre_train_out(d_char_out)
        return pre_score, hidden

    def forward(
        self, forw_sentence, forw_position, back_sentence, 
        back_position, word_seq, word_pos, word_dict, file_no, hidden=None):
        '''
        args:
            forw_sentence (char_seq_len, batch_size) : char-level representation of sentence
            forw_position (word_seq_len, batch_size) : position of blank space in char-level representation of sentence
            back_sentence (char_seq_len, batch_size) : char-level representation of sentence (inverse order)
            back_position (word_seq_len, batch_size) : position of blank space in inversed char-level representation of sentence
            word_seq (word_seq_len, batch_size) : word-level representation of sentence
            word_pos (word_seq_len, batch_size): position of blank space in word-level representation of sentence
            hidden: initial hidden state

        return:
            crf output (word_seq_len, batch_size, tag_size, tag_size), hidden
        '''

        self.set_batch_seq_size(forw_position)

        #embedding layer
        forw_emb = self.char_embeds(forw_sentence)
        back_emb = self.char_embeds(back_sentence)

        #dropout
        d_f_emb = self.dropout(forw_emb)
        d_b_emb = self.dropout(back_emb)

        #forward the whole sequence
        forw_lstm_out, _ = self.forw_char_lstm(d_f_emb)#seq_len_char * batch * char_hidden_dim

        back_lstm_out, _ = self.back_char_lstm(d_b_emb)#seq_len_char * batch * char_hidden_dim

        #select predict point
        forw_position = forw_position.unsqueeze(2).expand(self.word_seq_length, self.batch_size, self.char_hidden_dim)
        select_forw_lstm_out = torch.gather(forw_lstm_out, 0, forw_position)

        back_position = back_position.unsqueeze(2).expand(self.word_seq_length, self.batch_size, self.char_hidden_dim)
        select_back_lstm_out = torch.gather(back_lstm_out, 0, back_position)

        fb_lstm_out = self.dropout(torch.cat((select_forw_lstm_out, select_back_lstm_out), dim=2))
        if self.if_highway:
            char_out = self.fb2char(fb_lstm_out)
            d_char_out = self.dropout(char_out)
        else:
            d_char_out = fb_lstm_out

        #word
        # print("word_seq: ", word_seq)
        word_emb = self.word_embeds(word_seq)
        d_word_emb = self.dropout(word_emb) #(word_seq_length, batch_size, embedding_dim)
        # print("word_emb: ", d_word_emb.shape)
        # print("word_level_attention: ", self.word_level_attention)
        if self.word_level_attention:
            word_pos_enc = self.position_enc(word_pos) #(word_seq_length, batch_size, embedding_dim)
            # print("word_pos_enc: ", word_pos_enc.shape)
            #combine
            enc_output = torch.cat((d_word_emb, d_char_out, word_pos_enc), dim = 2).permute(1, 0, 2)
            # print("enc_output: ", enc_output.shape)
            #(batch_size, word_seq_length, embedding_dim*2+char_lstm_output_dim)

            #prepare masks
            slf_attn_mask = utils.get_attn_key_pad_mask(seq_k=word_seq.permute(1,0), seq_q=word_seq.permute(1,0), word_dict=word_dict)
            # print("slf_attn_mask: ", slf_attn_mask.shape)
            non_pad_mask = utils.get_non_pad_mask(seq=word_seq.permute(1,0), word_dict=word_dict)
            # print("non_pad_mask: ", non_pad_mask.shape)

            #pass multi-head-attention layers
            for enc_layer in self.layer_stack:
                enc_output, _ = enc_layer(
                    enc_output,
                    non_pad_mask=non_pad_mask,
                    slf_attn_mask=slf_attn_mask
                )
            
            # print("enc_output: ", enc_output.shape)
            #(batch_size, word_seq_length, embedding_dim*2+char_lstm_output_dim)

            fc_out = self.fc(enc_output)
            # print("fc_out: ", fc_out.shape)
            #convert to crf
            crf_out = self.crflist[file_no](fc_out)
            # print("crf_out: ", crf_out.shape)
        else:
            #combine
            word_input = torch.cat((d_word_emb, d_char_out), dim = 2)

            #word level lstm
            lstm_out, _ = self.word_lstm(word_input)
            d_lstm_out = self.dropout(lstm_out)

            #convert to crf
            crf_out = self.crflist[file_no](d_lstm_out)

        crf_out = crf_out.view(self.batch_size, self.word_seq_length, self.tagset_size, self.tagset_size)
        crf_out = crf_out.permute(1,0,2,3)
        # print(crf_out)
        return crf_out