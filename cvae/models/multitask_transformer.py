import torch
import torch.nn as nn
import torch.nn.functional as F
import torch, torch.nn as nn, torch.nn.functional as F
import torch.utils.data
import math
import pathlib
import tqdm

from cvae.tokenizer.selfies_property_val_tokenizer import SelfiesPropertyValTokenizer
import cvae.utils

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        pe = pe.unsqueeze(0)  # Shape: [1, max_len, d_model]
        self.register_buffer('pe', pe)

    def forward(self, x):
        # x shape: [batch_size, sequence_length, d_model]
        # Update positional encoding to match batch size and sequence length
        x = x + self.pe[:, :x.size(1)].repeat(x.size(0), 1, 1)
        return self.dropout(x)

def generate_square_subsequent_mask(sz: int) -> torch.Tensor:
    """ Generate the attention mask for causal decoding """
    mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1).float()
    mask = mask.masked_fill(mask == 0, float("-inf")).masked_fill(mask == 1, float(0.0))
    return mask

def generate_custom_subsequent_mask(sz: int) -> torch.Tensor:
    """ Generate a custom attention mask for causal decoding with specific unmasked positions """
    mask = generate_square_subsequent_mask(sz)
    
    for i in range(1, sz-1, 2):
        mask[i,i+1] = 0.0
    
    return mask

def generate_static_mask(selfies_sz: int, assayval_sz:int) -> torch.Tensor:
    sf_sf_mask = generate_square_subsequent_mask(selfies_sz)
    sf_av_mask = torch.zeros((selfies_sz, assayval_sz)) # no mask
    av_sf_mask = torch.zeros((assayval_sz, selfies_sz)) # no mask
    
    av_av_sz = assayval_sz // 2
    av_av_mask = torch.tril(torch.ones((av_av_sz,av_av_sz), dtype=torch.bool)).repeat_interleave(2, dim=1).repeat_interleave(2,dim=0).float()
    av_av_mask = av_av_mask.masked_fill(av_av_mask == 1., float(0.0)).masked_fill(av_av_mask == 0., float("-inf"))
    
    mask = torch.cat([torch.cat([sf_sf_mask, sf_av_mask], dim=1), torch.cat([av_sf_mask, av_av_mask], dim=1)], dim=0) 
    
    return mask
    
class MultitaskTransformer(nn.Module):
    
    def __init__(self, tokenizer, hdim=512, nhead=4, num_layers=4, dim_feedforward=512, dropout_rate=0.1, output_size=None):
        
        super().__init__()
        
        self.output_size = tokenizer.vocab_size if output_size is None else output_size
        self.hdim = hdim
        self.nhead = nhead
        self.dim_feedforward = dim_feedforward
        self.tokenizer = tokenizer
        self.token_pad_idx = tokenizer.PAD_IDX
        
        self.embedding = nn.Embedding(tokenizer.vocab_size, self.hdim)
        self.positional_encoding = PositionalEncoding(self.hdim)
        
        encode_args = {"d_model": self.hdim, "nhead": self.nhead, "dim_feedforward": self.dim_feedforward, "dropout": dropout_rate}
        encode_layer = nn.TransformerEncoderLayer(**encode_args, batch_first=True)
        self.encoder = nn.TransformerEncoder(encode_layer, num_layers=num_layers)
        
        decode_args = {"d_model": self.hdim, "nhead": self.nhead, "dim_feedforward": self.dim_feedforward, "dropout": dropout_rate}
        decode_layer = nn.TransformerDecoderLayer(**decode_args, batch_first=True)
        self.decoder = nn.TransformerDecoder(decode_layer, num_layers=num_layers)
        self.decoder_norm = nn.LayerNorm(self.hdim)
        
        self.classification_layers = nn.Sequential(
            nn.Linear(self.hdim, self.output_size),  # First layer upscales to dim_feedforward
            nn.LeakyReLU(),  # Nonlinear activation
            nn.Linear(self.output_size, self.output_size),  # Further processing layer
            nn.LeakyReLU(),  # Nonlinear activation
            nn.Linear(self.output_size, self.output_size)
        )


    def forward(self, input, teach_forcing):
        
        memory_mask = input == self.token_pad_idx
        
        input_embedding = self.positional_encoding(self.embedding(input))
        input_encoding = self.encoder(input_embedding, src_key_padding_mask=memory_mask)
        
        teach_forcing = self.positional_encoding(self.embedding(teach_forcing))
        tgt_mask = generate_custom_subsequent_mask(teach_forcing.size(1)).to(input.device)
        
        decoded = self.decoder(teach_forcing, input_encoding, tgt_mask=tgt_mask, memory_key_padding_mask=memory_mask)
        decoded = self.decoder_norm(decoded)
        
        logits = self.classification_layers(decoded)
        
        return logits
    
    @staticmethod
    def lossfn(ignore_index = -100, weight_decay=1e-5):
        ce_lossfn = nn.CrossEntropyLoss(reduction='mean', ignore_index=ignore_index, label_smoothing=0.05)
        def lossfn(parameters, logits, output):
            ce_loss = ce_lossfn(logits, output)
            return ce_loss
        return lossfn   
    
    def save(self, path):
        if not isinstance(path, pathlib.Path):
            path = pathlib.Path(path)
        
        cvae.utils.mk_empty_directory(path, overwrite=True)
        cvae.utils.mk_empty_directory(path / "spvt_tokenizer", overwrite=True)
        self.tokenizer.save(path / "spvt_tokenizer")
        torch.save(self.state_dict(), path / "mtransformer.pt")
        return path
    
    @staticmethod
    def load(dirpath = pathlib.Path("brick/mtransform1")):
        dirpath = pathlib.Path(dirpath)
        tokenizer = SelfiesPropertyValTokenizer.load(dirpath / "spvt_tokenizer")
        model = MultitaskTransformer(tokenizer)
        model.load_state_dict(torch.load(dirpath / 'mtransformer.pt'))
        model.eval()
        return model

class SequenceShiftDataset(torch.utils.data.Dataset):

    def __init__(self, path, tokenizer: SelfiesPropertyValTokenizer, nprops=5, assay_filter=[]):
        self.nprops = nprops
        self.assay_filter = assay_filter
        self.data = []
        self.cumulative_lengths = [0]
        cumulative_length = 0
        self.tokenizer= tokenizer
        self.pad_idx, self.sep_idx, self.end_idx = tokenizer.PAD_IDX, tokenizer.SEP_IDX, tokenizer.END_IDX
        

        # file_path = next(pathlib.Path(path).glob("*.pt"))
        for file_path in tqdm.tqdm(pathlib.Path(path).glob("*.pt")):
            file_data = torch.load(file_path)
            
            # num_props = file_data['assay_vals'].size(1)
            # assay_vals = file_data['assay_vals'][num_props > 9]
            self.data.extend([(file_data['selfies'], file_data['assay_vals'])])
            cumulative_length += file_data['selfies'].size(0)
            self.cumulative_lengths.append(cumulative_length)

    def __len__(self):
        return self.cumulative_lengths[-1] if self.cumulative_lengths else 0

    def __getitem__(self, idx):
        
        # Find which section this index falls into and update the index to be relative to that section
        file_idx = next(i for i, total_length in enumerate(self.cumulative_lengths) if total_length > idx) - 1
        idx -= self.cumulative_lengths[file_idx]
        
        idxdata = self.data[file_idx]
        selfies_raw, raw_assay_vals = idxdata[0][idx], idxdata[1][idx]
        
        # remove padding from selfies
        # selfies = selfies_raw[selfies_raw != self.pad_idx]

        # assay_val munging - unpad, randomly permute, add sos/eos tokens
        assay_vals = raw_assay_vals[raw_assay_vals != self.pad_idx][1:-1]
        reshaped_av = assay_vals.reshape(assay_vals.size(0) // 2, 2)
        av_shuffled = reshaped_av[torch.randperm(reshaped_av.size(0)),:].reshape(assay_vals.size(0))
        
        # truncate to n_features random features
        n_features = self.nprops
        av_truncate = av_shuffled[0:(n_features*2)]
        
        # add start and end tokends and pad to 120 length
        av_sos_eos = torch.cat([torch.LongTensor([self.sep_idx]), av_truncate, torch.LongTensor([self.end_idx])])
        
        # add padding up to n_features*2+2
        out = F.pad(av_sos_eos, (0, (n_features*2+2) - av_sos_eos.size(0)), value=self.pad_idx)

        # out = torch.hstack([av_truncate,torch.tensor([self.pad_idx])])
        tch = torch.hstack([torch.tensor([1]),out[:-1]])
        
        # inp = selfies_raw # F.pad(selfies, (0, 119 - selfies.size(0)), value=self.pad_idx)
        # inp = torch.hstack([selfies, torch.tensor([self.sep_idx]), av_shuffled[2:4]])
        inp = selfies_raw
        # pad_inp = F.pad(inp, (0, 126 - inp.size(0)), value=self.pad_idx)
        
        return inp, tch, out

class LabelSmoothingCrossEntropySequence(nn.Module):
    def __init__(self, epsilon_ls=0.1, ignore_index=None):
        super(LabelSmoothingCrossEntropySequence, self).__init__()
        self.epsilon_ls = epsilon_ls
        self.ignore_index = ignore_index

    def forward(self, out, tgt):
        num_classes = out.size(-1)
        batch_size, seq_len = tgt.size()
        fill = self.epsilon_ls / (num_classes - 1)
        dev = out.device
        
        with torch.no_grad():
            # Create smoothed label
            smooth_label = torch.full(size=(batch_size, seq_len, num_classes), fill_value=fill, device=dev)
            tgt = tgt.unsqueeze(-1)  # Add an extra dimension for scatter_
            smooth_label.scatter_(-1, tgt, 1.0 - self.epsilon_ls)

            if self.ignore_index is not None:
                # Create a mask for ignoring the index
                ignore_mask = tgt.eq(self.ignore_index)
                smooth_label.masked_fill_(ignore_mask, 0.0)

        # Calculate cross-entropy loss with the smoothed labels
        loss = -smooth_label * F.log_softmax(out, dim=-1)
        
        if self.ignore_index is not None:
            # Apply the ignore mask to the loss
            loss.masked_fill_(ignore_mask, 0.0)

        loss = loss.sum(dim=-1)  # Sum over classes
        # Only average over non-ignored elements
        loss = loss.masked_select(~ignore_mask.squeeze(-1)).mean()
        
        return loss

class NoamLR(torch.optim.lr_scheduler._LRScheduler):

    def __init__(self, optimizer, model_size, warmup_steps, last_epoch=-1, min_lr=1e-6, max_lr=1e-3):
        self.model_size = model_size
        self.warmup_steps = warmup_steps
        self.min_lr = min_lr
        self.max_lr = max_lr
        super(NoamLR, self).__init__(optimizer, last_epoch)

    def get_lr(self):
        step_num = self.last_epoch + 1
        lr = self.model_size ** (-0.5) * min(step_num ** (-0.5), step_num * self.warmup_steps ** (-1.5))
        lr = max(self.min_lr, min(lr, self.max_lr))
        return [base_lr * lr for base_lr in self.base_lrs]
