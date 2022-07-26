import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from strategies.cfg import NNCFG

from pytorch_lightning import LightningDataModule, LightningModule, Trainer, seed_everything
from pytorch_lightning.loggers import CSVLogger
from pytorch_lightning.callbacks import ModelCheckpoint

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from sklearn.model_selection import train_test_split


class NNRatiosRegression(LightningModule):
    def __init__(self, input_shape,
                 hidden_shape=NNCFG.hidden_shape):
        super().__init__()
        self.layer_1 = nn.Linear(input_shape, hidden_shape)
        self.activation = nn.Tanh()
        self.layer_2 = nn.Linear(hidden_shape, 1)

        self.loss = nn.MSELoss()

    def __repr__(self):
        return f'NNRatiosRegression(hidden_shape={NNCFG.hidden_shape})'

    def forward(self, x):
        x = self.layer_1(x)
        x = self.activation(x)
        x = self.layer_2(x)
        return x

    def configure_optimizers(self):
        optimizer = torch.optim.SGD(self.parameters(), lr=NNCFG.learning_rate, momentum=NNCFG.momentum)
        return optimizer

    def training_step(self, batch, batch_idx):
        x, y = batch
        out = self(x)
        train_loss = self.loss(out, y)
        if NNCFG.log_loss:
            self.log("train_loss", train_loss)
        return train_loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        out = self(x)
        val_loss = self.loss(out, y)
        if NNCFG.log_loss:
            self.log("val_loss", val_loss)



class DataModule(LightningDataModule):
    def __init__(self, data_x, data_y):
        super().__init__()


        self.data_x = torch.Tensor(data_x.to_numpy())
        self.data_y = torch.Tensor(data_y.to_numpy().reshape(-1,1))

        self.data_x = torch.nan_to_num(self.data_x)
        self.data_y = torch.nan_to_num(self.data_y)

        self.x_tr, self.x_val, self.y_tr, self.y_val = train_test_split(self.data_x, self.data_y, test_size=NNCFG.val_size)

    def train_dataloader(self):
        dataset = TensorDataset(self.x_tr, self.y_tr)
        return DataLoader(dataset, batch_size=NNCFG.batch_size,  shuffle=True)

    def val_dataloader(self):
        dataset = TensorDataset(self.x_val, self.y_val)
        return DataLoader(dataset, batch_size=NNCFG.batch_size)


class NNRatios(BaseStrategy):
    def __init__(self):
        """
        NNRatios implements the trading strategy using the Neural Network
        as outlined in the paper "Neural network forecasts of Canadian stock
        returns using accounting ratios", but done in the simplified form.

        Args:
        required_number_dates : specifies the required number of dates needed
        to for each evaluation step

        decision_rule : specifies how the portfolio is formed
        'median' <- all stocks returns which are forecasted above median are
        bought long, and all stocks forecasted below are sold short
        'quartile' <- all stocks are bought from the first quartile,
        and all the stocks sold are from the last quartile, the
        remaining are not entered into the portfolio
        'octile' <- the same strategy as for 'quartile' but using
        the octiles for decision rules

        train_interval : specifies the intervals when the linear
        regression must be trained before being evaluated, otherwise
        the pretrained regression is used

        column_y : name of the prediction column
        columns_x : names of the data columns
        """
        super().__init__(required_number_dates=NNCFG.required_number_dates)

        self.decision_rule = NNCFG.decision_rule
        assert self.decision_rule in ['median', 'quartile', 'octile'], ('Warning,'
                                         'Decision rule is specified incorrectly!')

        self.train_interval = True

        self.column_y = 'return'
        self.columns_x = ['outstanding_share', 'turnover', 'pe', 'pe_ttm', 'pb',
                          'ps', 'ps_ttm', 'dv_ratio', 'dv_ttm', 'total_mv', 'qfq_factor']

        self.model = NNRatiosRegression(input_shape=len(self.columns_x))

        self.csv_logger = CSVLogger("./nn_logs", name=repr(self.model))

        self.checkpoint_callback = ModelCheckpoint(monitor='val_loss',
                                                   mode='min',
                                                   dirpath='./pretrained_models/',
                                                   filename=repr(self.model))

        self.trainer = Trainer(logger=self.csv_logger,
                               log_every_n_steps=NNCFG.log_frequency,
                               max_epochs=NNCFG.epochs,
                               val_check_interval=NNCFG.val_check_interval,
                               callbacks=self.checkpoint_callback,
                               fast_dev_run=False)

    def _prepare_data(self, strategy_data):
        new_df = pd.DataFrame(columns=strategy_data.columns)
        for ticker in strategy_data['ticker'].unique():
            current_df = strategy_data[strategy_data['ticker'] == ticker]
            current_df = current_df.sort_values(by=['date'])
            current_df['next_price'] = current_df['price'].diff().shift(-1)
            current_df['return'] = current_df.apply(lambda x: x['next_price'] / x['price'] - 1, axis=1)
            current_df = current_df.fillna(0)
            new_df = pd.concat([new_df, current_df])

        train_y = new_df[self.column_y]
        train_x = new_df[self.columns_x]
        return train_x, train_y


    def create_portfolio(self, strategy_data, available_tickers) -> dict:
        # If self.train_interval is true, then train the model
        if self.train_interval:
            train_x, train_y = self._prepare_data(strategy_data)
            dm = DataModule(train_x, train_y)
            self.trainer.fit(model=self.model,datamodule=dm)
            self.train_interval = False

        # Perform the formation of the portfolio
        latest_date = max(strategy_data['date'].unique())
        latest_data = strategy_data[( strategy_data['date'] == latest_date ) &
                                    ( strategy_data['ticker'].isin(available_tickers) )].dropna()

        pred_x = latest_data[self.columns_x]
        pred_x = torch.Tensor(pred_x.to_numpy())
        pred_tickers = latest_data['ticker']

        preds = self.model(pred_x)
        preds = preds.detach().numpy()
        preds = pd.DataFrame(preds, index=pred_tickers, columns=['prediction'])

        if self.decision_rule == 'median':
            upper_cutoff = np.median(preds)
            lower_cutoff = np.median(preds)

        elif self.decision_rule == 'quartile':
            upper_cutoff = np.quantile(preds, .75)
            lower_cutoff = np.quantile(preds, .25)

        elif self.decision_rule == 'octile':
            upper_cutoff = np.quantile(preds, .875)
            lower_cutoff = np.quantile(preds, .125)

        preds['prediction'] = [1 if x>upper_cutoff else -1 if x<lower_cutoff else 0 for x in preds['prediction']]

        # Count the values of long and short
        number_long = preds[preds['prediction'] == 1]['prediction'].shape[0]
        number_short = preds[preds['prediction'] == -1]['prediction'].shape[0]

        if number_long > 0:
            preds.loc[preds['prediction'] == 1, 'prediction'] = 1 / number_long

        if number_short > 0:
            preds.loc[preds['prediction'] == -1, 'prediction'] = -1 / number_short

        return preds.to_dict()['prediction']