""" A script that contains model classes to be used in training."""
import os

import torch
from torch import nn
from transformers import BertPreTrainedModel, AutoConfig, RobertaPreTrainedModel, BertModel, XLMRobertaModel
from transformers.modeling_outputs import SequenceClassifierOutput

from utils import cos_sim


class Pooler(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        pooled_output = self.dense(hidden_states)
        pooled_output = self.activation(pooled_output)
        return pooled_output


# TODO: maybe inherit nn.Module?
class LowerXLMREncoder(RobertaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.roberta = XLMRobertaModel(config)
        self.pooler = Pooler(config)
        # TODO: change to post_init()
        self.init_weights()

    def forward(self, input_ids, token_type_ids=None, attention_mask=None):
        model_output = self.base_model(input_ids, attention_mask=attention_mask, token_type_ids=None)
        output = model_output['last_hidden_state'][:, 0]  # (batch_size, hidden_size)
        output = self.pooler(output)
        return output


class LowerBertEncoder(BertPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)
        self.bert = BertModel(config)
        self.pooler = Pooler(config)
        # TODO: change to post_init()
        self.init_weights()

    def forward(self, input_ids, token_type_ids=None, attention_mask=None):
        model_output = self.base_model(input_ids, attention_mask=attention_mask, token_type_ids=None)
        output = model_output['last_hidden_state'][:, 0]  # (batch_size, hidden_size)
        output = self.pooler(output)
        return output


class HiearchicalBaseModel(nn.Module):
    # To be used for sliding window approaches
    def __init__(self, args, tokenizer, **kwargs):
        super().__init__()
        # TODO: from pretrained or config
        self.lower_config = AutoConfig.from_pretrained(args.model_name_or_path)
        self.lower_model = self.lower_selector(args.model_name_or_path)
        self.lower_dropout = nn.Dropout(args.lower_dropout)
        
        # If True, freeze the lower encoder
        if args.frozen:
            self._freeze_lower()
    

    def forward(self, input_ids, token_type_ids=None, attention_mask=None):
        input_ids = input_ids.permute(1, 0, 2)  # (sentences, batch_size, words)
        attention_mask = attention_mask.permute(1, 0, 2)
        lower_encoded = []

        for i_i, a_m in zip(input_ids, attention_mask):
            inter_output = self.lower_model(i_i, a_m)
            inter_output = self.lower_dropout(inter_output)
            lower_encoded.append(inter_output)

        lower_output = torch.stack(lower_encoded)  # (sentences, batch_size, hidden_size)
        lower_output = lower_output.permute(1, 0, 2)  # (batch_size, sentences, hidden_size)
       
        # Mean Pooling
        final_output = torch.mean(lower_output, 1)  # (batch_size, hidden_size)

        return final_output

    def _freeze_lower(self):
        for param in self.lower_model.base_model.parameters():
            param.requires_grad = False
        
    def lower_selector(self, model_name):
        if self.lower_config.model_type == "xlm-roberta":
            lower_model = LowerXLMREncoder.from_pretrained(model_name)
        elif self.lower_config.model_type == "bert":
            lower_model = LowerBertEncoder.from_pretrained(model_name)
        else:
            raise NotImplementedError("Respective model type is not supported.")
        return lower_model


# TODO: Inherit from HiearchicalBaseModel, memory issues
class HiearchicalModel(nn.Module):
    # self.lower_model.base_model is a reference to self.lower_model.bert
    def __init__(self, args, tokenizer, **kwargs):
        super().__init__()
        # TODO: from pretrained or config
        self.lower_config = AutoConfig.from_pretrained(args.model_name_or_path)
        self.lower_model = self.lower_selector(args.model_name_or_path)
        self.lower_dropout = nn.Dropout(args.lower_dropout)

        self.tokenizer = tokenizer
        self.lower_model.resize_token_embeddings(len(self.tokenizer))

        self.encoder_layer = nn.TransformerEncoderLayer(d_model=self.lower_config.hidden_size,
                                                        nhead=args.upper_nhead,
                                                        dim_feedforward=args.upper_dim_feedforward,
                                                        dropout=args.upper_dropout,
                                                        activation=args.upper_activation,
                                                        layer_norm_eps=args.upper_layer_norm_eps,
                                                        batch_first=True)
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer=self.encoder_layer,
                                                         num_layers=args.upper_num_layers)

        # If True, freeze the lower encoder
        if args.frozen:
            self._freeze_lower()

        # If positional encoding will be used in upper encoder or not
        self.upper_positional = getattr(args, "upper_positional", True)

    def forward(self, input_ids, token_type_ids=None, attention_mask=None):
        input_ids = input_ids.permute(1, 0, 2)  # (sentences, batch_size, words)
        attention_mask = attention_mask.permute(1, 0, 2)
        lower_encoded = []

        for i_i, a_m in zip(input_ids, attention_mask):
            inter_output = self.lower_model(i_i, a_m)
            inter_output = self.lower_dropout(inter_output)
            lower_encoded.append(inter_output)

        lower_output = torch.stack(lower_encoded)  # (sentences, batch_size, hidden_size)
        lower_output = lower_output.permute(1, 0, 2)  # (batch_size, sentences, hidden_size)

        # Modified: Document level [CLS] tokens are prepended to the documents
        dcls_tokens = self.tokenizer(["[DCLS]"] * lower_output.shape[0],
                                     add_special_tokens=False,
                                     return_tensors="pt",
                                     return_attention_mask=False,
                                     return_token_type_ids=False)
        # TODO: Maybe create random tensors instead?
        dcls_tokens.to(lower_output.device)
        dcls_out = self.lower_model.base_model.embeddings.word_embeddings(dcls_tokens["input_ids"])
        lower_output = torch.cat([dcls_out, lower_output], dim=1)

        if self.upper_positional:
            lower_output = self.lower_model.base_model.embeddings(inputs_embeds=lower_output)

        upper_output = self.transformer_encoder(lower_output)  # (batch_size, sentences, hidden_size)
        final_output = upper_output[:, 0]  # (batch_size, hidden_size)

        return final_output

    def _freeze_lower(self):
        for param in self.lower_model.base_model.parameters():
            param.requires_grad = False
    
    def lower_selector(self, model_name):
        if self.lower_config.model_type == "xlm-roberta":
            lower_model = LowerXLMREncoder.from_pretrained(model_name)
        elif self.lower_config.model_type == "bert":
            lower_model = LowerBertEncoder.from_pretrained(model_name)
        else:
            raise NotImplementedError("Respective model type is not supported.")
        return lower_model


class ContrastiveModel(nn.Module):
    def __init__(self, args, tokenizer, **kwargs):
        super().__init__()
        self.hierarchical_model = HiearchicalModel(args, tokenizer)
        self.scale = args.scale
        self.use_hard_negatives = args.use_hard_negatives
        self.cross_entropy_loss = nn.CrossEntropyLoss()

        if args.similarity_fct == "cos_sim":
            self.similarity_fct = cos_sim
        else:
            raise NotImplementedError("Respective similarity function is not implemented.")

    def forward(self, article_1, mask_1, article_2, mask_2, article_3=None, mask_3=None, article_4=None, mask_4=None):
        output_1 = self.hierarchical_model(input_ids=article_1,
                                           attention_mask=mask_1)  # (batch_size, hidden_size)
        output_2 = self.hierarchical_model(input_ids=article_2,
                                           attention_mask=mask_2)  # (batch_size, hidden_size)
        if self.use_hard_negatives:
            output_3 = self.hierarchical_model(input_ids=article_3,
                                               attention_mask=mask_3)  # (batch_size, hidden_size)
            output_4 = self.hierarchical_model(input_ids=article_4,
                                               attention_mask=mask_4)  # (batch_size, hidden_size)

            scores_1 = self.similarity_fct(output_1, torch.cat([output_2, output_3])) * self.scale
            scores_2 = self.similarity_fct(output_2, torch.cat([output_1, output_4])) * self.scale
        else:
            scores_1 = self.similarity_fct(output_1, output_2) * self.scale
            scores_2 = self.similarity_fct(output_2, output_1) * self.scale

        labels = torch.tensor(range(len(scores_1)), dtype=torch.long, device=scores_1.device)
        return self.cross_entropy_loss(scores_1, labels) + self.cross_entropy_loss(scores_2, labels)


class HierarchicalClassificationModel(nn.Module):
    def __init__(self, c_args, args, tokenizer, num_labels, **kwargs):
        super().__init__()
        self.hierarchical_model = HiearchicalModel(args, tokenizer)

        if c_args.custom_model == "hierarchical":
            self.hierarchical_model = HiearchicalModel(args, tokenizer)
            if not c_args.custom_from_scratch:
                self.hierarchical_model.load_state_dict(torch.load(os.path.join(c_args.pretrained_dir, "model.pth")))
        elif c_args.custom_model == "sliding_window":
            self.hierarchical_model = HiearchicalBaseModel(args, tokenizer)
        else:
            raise NotImplementedError("Respective model type is not supported.")

        self.num_labels = num_labels
        if c_args.dropout is not None:
            self.dropout = nn.Dropout(c_args.dropout)
        self.classifier = nn.Linear(self.hierarchical_model.lower_config.hidden_size, self.num_labels)

    def forward(self, article_1, mask_1, labels):
        output = self.hierarchical_model(input_ids=article_1, attention_mask=mask_1) # (batch_size, hidden_size)

        output = self.dropout(output)
        logits = self.classifier(output)

        if self.num_labels > 1 and (labels.dtype == torch.long or labels.dtype == torch.int):
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(logits.view(-1, self.num_labels), labels.view(-1))
        else:
            loss_fct = nn.BCEWithLogitsLoss()
            loss = loss_fct(logits, labels)

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits
        )
