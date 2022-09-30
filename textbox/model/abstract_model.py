import torch.nn as nn


class AbstractModel(nn.Module):
    r"""Base class for all models
    """

    def __init__(self, config, tokenizer):
        # load parameters info
        super(AbstractModel, self).__init__()
        self.device = config['device']
        self.config = config
        self.tokenizer = tokenizer

    def generate(self, batch_data, eval_data):
        r"""Predict the texts conditioned on a noise or sequence.

        Args:
            batch_data (Corpus): Corpus class of a single batch.
            eval_data: Common data of all the batches.

        Returns:
            torch.Tensor: Generated text, shape: [batch_size, max_len]
        """
        raise NotImplementedError

    def __str__(self):
        """
        Model prints with number of trainable parameters
        """
        params = sum(p.numel() for p in self.parameters() if p.requires_grad)
        return super().__str__() + '\nTrainable parameters: {}'.format(params)
    
    def forward(self, batch, epoch_idx=-1):
        inputs = {
            'input_ids': batch['source_ids'].to(self.device),
            'attention_mask': batch['source_mask'].to(self.device),
            'labels': batch['target_ids'].to(self.device)
        }
        outputs = self.model(**inputs)

        if self.label_smoothing:
            loss_fct = nn.CrossEntropyLoss(label_smoothing=self.label_smoothing)
            vocab_size = outputs.logits.size()[-1]
            return loss_fct(outputs.logits.view(-1, vocab_size), inputs['labels'].view(-1))
        else:
            return outputs.loss

    def generate(self, batch, eval_data, accelerator=None):
        inputs = {
            'input_ids': batch['source_ids'].to(self.device),
            'attention_mask': batch['source_mask'].to(self.device),
        }
        sample_outputs = accelerator.unwrap_model(self.model).generate(**inputs, **self.generation_kwargs)
        sample_outputs = accelerator.pad_across_processes(sample_outputs, dim=1, pad_index=self.tokenizer.pad_token_id)
        sample_outputs = accelerator.gather((sample_outputs))

        decode_kwargs = {'skip_special_tokens': True, 'clean_up_tokenization_spaces': False}
        generated_text = self.tokenizer.batch_decode(sample_outputs, **decode_kwargs)
        generated_text = [g.strip() or 'NULL' for g in generated_text]
        # return generated_text
        return generated_text
