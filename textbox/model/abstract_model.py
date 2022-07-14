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