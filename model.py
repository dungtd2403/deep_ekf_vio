import torch
import torch.nn as nn
import numpy as np
import data_loader
import torch_se3
import time
from params import par
from torch.autograd import Variable
from torch.nn.init import kaiming_normal_, orthogonal_


def conv(batchNorm, in_planes, out_planes, kernel_size=3, stride=1, dropout=0):
    if batchNorm:
        return nn.Sequential(
                nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=(kernel_size - 1) // 2,
                          bias=False),
                nn.BatchNorm2d(out_planes),
                nn.LeakyReLU(0.1, inplace=True),
                nn.Dropout(dropout)  # , inplace=True)
        )
    else:
        return nn.Sequential(
                nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size, stride=stride, padding=(kernel_size - 1) // 2,
                          bias=True),
                nn.LeakyReLU(0.1, inplace=True),
                nn.Dropout(dropout)  # , inplace=True)
        )


class IMUKalmanFilter(nn.Module):
    def __init__(self):
        super(IMUKalmanFilter, self).__init__()

    def force_symmetrical(self, M):
        M_upper = torch.triu(M)
        return M_upper + M_upper.transpose(-2, -1) * \
               (1 - torch.eye(M_upper.size(-2), M_upper.size(-1), device=M.device).repeat(M_upper.size(0), 1, 1))

    def predict_one_step(self, t_accum, C_accum, r_accum, v_accum, dt, g_k, v_k, bw_k, ba_k, covar,
                         gyro_meas, accel_meas, imu_noise_covar):
        mm = torch.matmul
        batch_size = dt.size(0)

        dt2 = dt * dt
        w = gyro_meas - bw_k
        w_skewed = torch_se3.skew3_b(w)
        C_accum_transpose = C_accum.transpose(-2, -1)
        a = accel_meas - ba_k
        v = mm(C_accum_transpose, v_k - g_k * t_accum + v_accum)
        v_skewed = torch_se3.skew3_b(v)
        I3 = torch.eye(3, 3, device=covar.device).repeat(batch_size, 1, 1)
        exp_int_w = torch_se3.exp_SO3_b(dt * w)
        exp_int_w_transpose = exp_int_w.transpose(-2, -1)

        # propagate uncertainty, 2nd order
        F = torch.zeros(batch_size, 18, 18, device=covar.device)
        F[:, 3:6, 3:6] = -w_skewed
        F[:, 3:6, 12:15] = -I3
        F[:, 6:9, 3:6] = -mm(C_accum, v_skewed)
        F[:, 6:9, 9:12] = C_accum
        F[:, 9:12, 0:3] = -C_accum_transpose
        F[:, 9:12, 3:6] = -torch_se3.skew3_b(mm(C_accum_transpose, g_k))
        F[:, 9:12, 9:12] = -w_skewed
        F[:, 9:12, 12:15] = -v_skewed
        F[:, 9:12, 15:18] = -I3

        G = torch.zeros(batch_size, 18, 12, device=covar.device)
        G[:, 3:6, 0:3] = -I3
        G[:, 9:12, 0:3] = -v_skewed
        G[:, 9:12, 6:9] = -I3
        G[:, 12:15, 3:6] = I3
        G[:, 15:18, 9:12] = I3

        Phi = torch.eye(18, 18, device=covar.device).repeat(batch_size, 1, 1) + \
              F * dt + 0.5 * mm(F, F) * dt2
        Phi[:, 6:9, 12:15] = torch.zeros(3, 3, device=covar.device)  # this blocks is exactly zero in 2nd order approx
        Phi[:, 3:6, 3:6] = exp_int_w_transpose
        Phi[:, 9:12, 9:12] = exp_int_w_transpose

        Q = mm(mm(mm(mm(Phi, G), imu_noise_covar.repeat(batch_size, 1, 1)),
                  G.transpose(-2, -1)), Phi.transpose(-2, -1)) * dt
        covar = mm(mm(Phi, covar), Phi.transpose(-2, -1)) + Q
        covar = self.force_symmetrical(covar)

        # propagate nominal states
        r_accum = r_accum + v_accum * dt + 0.5 * mm(C_accum, (dt2 * a))
        v_accum = v_accum + mm(C_accum, (dt * a))
        C_accum = mm(C_accum, exp_int_w)
        t_accum = t_accum + dt

        return t_accum, C_accum, r_accum, v_accum, covar, F, G, Phi, Q

    def predict(self, imu_meas, imu_noise_covar, prev_state, prev_covar):
        num_batches = imu_meas.size(0)
        C_accum = torch.eye(3, 3, device=imu_meas.device).repeat(num_batches, 1, 1)
        r_accum = torch.zeros(num_batches, 3, 1, device=imu_meas.device)
        v_accum = torch.zeros(num_batches, 3, 1, device=imu_meas.device)
        t_accum = torch.zeros(num_batches, 1, 1, device=imu_meas.device)
        pred_covar = prev_covar

        g_k, C_k, r_k, v_k, bw_k, ba_k = IMUKalmanFilter.decode_state_b(prev_state)
        for tau in range(0, imu_meas.size(1) - 1):
            t, gyro_meas, accel_meas = data_loader.SubseqDataset.decode_imu_data_b(imu_meas[:, tau, :])
            tp1, _, _ = data_loader.SubseqDataset.decode_imu_data_b(imu_meas[:, tau + 1, :])
            dt = tp1 - t
            sel = ~torch.isnan(dt).view(num_batches)

            # only update the selected batches
            # if torch.sum(sel) > 0:
            #     t_accum[sel], C_accum[sel], r_accum[sel], v_accum[sel], pred_covar[sel], _, _, _, _ = \
            #         self.predict_one_step(t_accum[sel], C_accum[sel], r_accum[sel], v_accum[sel], dt[sel], g_k[sel],
            #                               v_k[sel], bw_k[sel], ba_k[sel], pred_covar[sel],
            #                               gyro_meas[sel], accel_meas[sel], imu_noise_covar)
            t_accum, C_accum, r_accum, v_accum, pred_covar, _, _, _, _ = \
                self.predict_one_step(t_accum, C_accum, r_accum, v_accum, dt, g_k,
                                      v_k, bw_k, ba_k, pred_covar,
                                      gyro_meas, accel_meas, imu_noise_covar)

        pred_state = IMUKalmanFilter.encode_state_b(g_k,
                                                    torch.matmul(C_k, C_accum),
                                                    r_k + v_k * t_accum - 0.5 * g_k * t_accum * t_accum + r_accum,
                                                    torch.matmul(C_accum.transpose(-2, -1),
                                                                 v_k - g_k * t_accum + v_accum),
                                                    bw_k, ba_k)

        return pred_state, pred_covar

    def meas_residual_and_jacobi(self, C_pred, r_pred, vis_meas, T_imu_cam):
        C_cal = T_imu_cam[:, 0:3, 0:3]
        C_cal_transpose = C_cal.transpose(-2, -1)
        r_cal = T_imu_cam[:, 0:3, 3:4]

        mm = torch.matmul
        vis_meas_rot = vis_meas[:, 0:3, :]
        vis_meas_trans = vis_meas[:, 3:6, :]
        residual_rot = torch_se3.log_SO3_b(mm(mm(mm(torch_se3.exp_SO3_b(vis_meas_rot), C_cal_transpose),
                                                 C_pred.transpose(-2, -1)), C_cal))
        # residual_rot = vis_meas_rot - torch_se3.log_SO3_b(mm(mm(C_cal_transpose, C_pred), C_cal))
        residual_trans = vis_meas_trans - mm(mm(C_cal_transpose, C_pred), r_cal) - \
                         mm(C_cal_transpose, r_pred - r_cal)
        residual = torch.cat([residual_rot, residual_trans], dim=1)

        H = torch.zeros(vis_meas.shape[0], 6, 18, device=vis_meas.device)
        H[:, 0:3, 3:6] = -mm(mm(torch_se3.J_left_SO3_inv_b(-residual_rot), C_cal_transpose), C_pred)
        H[:, 3:6, 3:6] = mm(mm(C_cal_transpose, C_pred), torch_se3.skew3_b(r_cal))
        H[:, 3:6, 6:9] = -C_cal_transpose

        return residual, H

    def update(self, pred_state, pred_covar, vis_meas, vis_meas_covar, T_imu_cam):
        mm = torch.matmul
        g_pred, C_pred, r_pred, v_pred, bw_pred, ba_pred = IMUKalmanFilter.decode_state_b(pred_state)
        residual, H = self.meas_residual_and_jacobi(C_pred, r_pred, vis_meas, T_imu_cam)

        H = -H  # this is required for EKF, since the way we derived the Jacobian are for batch methods
        H_transpose = H.transpose(-2, -1)

        S = mm(mm(H, pred_covar), H_transpose) + vis_meas_covar
        K = mm(mm(pred_covar, H_transpose), S.inverse())

        est_error = mm(K, residual)

        I18 = torch.eye(18, 18, device=pred_state.device).repeat(vis_meas.size(0), 1, 1)
        est_covar = mm(I18 - mm(K, H), pred_covar)

        g_err = est_error[:, 0:3]
        C_err = est_error[:, 3:6]
        r_err = est_error[:, 6:9]
        v_err = est_error[:, 9:12]
        bw_err = est_error[:, 12:15]
        ba_err = est_error[:, 15:18]

        est_state = IMUKalmanFilter.encode_state_b(g_pred + g_err,
                                                   mm(C_pred, torch_se3.exp_SO3_b(C_err)),
                                                   r_pred + r_err,
                                                   v_pred + v_err,
                                                   bw_pred + bw_err,
                                                   ba_pred + ba_err)
        return est_state, est_covar

    def composition(self, prev_pose, est_state, est_covar):
        batch_size = est_state.size(0)
        g, C, r, v, bw, ba = IMUKalmanFilter.decode_state_b(est_state)
        C_transpose = C.transpose(-2, -1)

        new_pose = torch.eye(4, 4, device=prev_pose.device).repeat(batch_size, 1, 1)
        new_pose[:, 0:3, 0:3] = torch.matmul(C_transpose, prev_pose[:, 0:3, 0:3])
        new_pose[:, 0:3, 3:4] = torch.matmul(C_transpose, prev_pose[:, 0:3, 3:4] - r)
        new_g = torch.matmul(C_transpose, g)

        new_state = IMUKalmanFilter.encode_state_b(new_g,
                                                   torch.eye(3, 3, device=prev_pose.device).repeat(batch_size, 1, 1),
                                                   torch.zeros(batch_size, 3, 1, device=prev_pose.device),
                                                   v, bw, ba)
        U = torch.zeros(batch_size, 18, 18, device=prev_pose.device)
        U[:, 0:3, 0:3] = C_transpose
        U[:, 0:3, 3:6] = torch_se3.skew3_b(new_g)
        U[:, 9:18, 9:18] = torch.eye(9, 9, device=prev_pose.device)
        new_covar = torch.matmul(torch.matmul(U, est_covar), U.transpose(-2, -1))
        new_covar = self.force_symmetrical(new_covar)

        return new_pose, new_state, new_covar

    def forward(self, imu_data, imu_noise_covar,
                prev_pose, prev_state, prev_covar,
                vis_meas, vis_meas_covar, T_imu_cam):

        num_timesteps = vis_meas.size(1)  # equals to imu_data.size(1) - 1

        poses_over_timesteps = [prev_pose]
        states_over_timesteps = [prev_state]
        covars_over_timesteps = [prev_covar]
        for k in range(0, num_timesteps):
            pred_state, pred_covar = self.predict(imu_data[:, k], imu_noise_covar,
                                                  states_over_timesteps[-1], covars_over_timesteps[-1])
            est_state, est_covar = self.update(pred_state, pred_covar,
                                               vis_meas[:, k], vis_meas_covar[:, k], T_imu_cam)
            new_pose, new_state, new_covar = self.composition(poses_over_timesteps[-1], est_state, est_covar)

            poses_over_timesteps.append(new_pose)
            states_over_timesteps.append(new_state)
            covars_over_timesteps.append(new_covar)

        return torch.stack(poses_over_timesteps, 1), \
               torch.stack(states_over_timesteps, 1), \
               torch.stack(covars_over_timesteps, 1)

    @staticmethod
    def decode_state_b(state_vector):
        g = state_vector[..., 0:3].view(-1, 3, 1)
        C = state_vector[..., 3:12].view(-1, 3, 3)
        r = state_vector[..., 12:15].view(-1, 3, 1)
        v = state_vector[..., 15:18].view(-1, 3, 1)
        bw = state_vector[..., 18:21].view(-1, 3, 1)
        ba = state_vector[..., 21:24].view(-1, 3, 1)

        return g, C, r, v, bw, ba

    @staticmethod
    def encode_state_b(g, C, r, v, bw, ba):
        return torch.cat((g.view(-1, 3),
                          C.view(-1, 9), r.view(-1, 3),
                          v.view(-1, 3),
                          bw.view(-1, 3), ba.view(-1, 3),), 1)

    @staticmethod
    def encode_state(g, C, r, v, bw, ba):
        return torch.squeeze(IMUKalmanFilter.encode_state_b(g, C, r, v, bw, ba))

    @staticmethod
    def decode_state(state_vector):
        g, C, r, v, bw, ba = IMUKalmanFilter.decode_state_b(state_vector)
        return g.view(3, 1), C.view(3, 3), r.view(3, 1), v.view(3, 1), bw.view(3, 1), ba.view(3, 1)


class DeepVO(nn.Module):
    def __init__(self, imsize1, imsize2, batchNorm):
        super(DeepVO, self).__init__()
        # CNN
        self.batchNorm = batchNorm
        self.conv1 = conv(self.batchNorm, 6, 64, kernel_size=7, stride=2, dropout=par.conv_dropout[0])
        self.conv2 = conv(self.batchNorm, 64, 128, kernel_size=5, stride=2, dropout=par.conv_dropout[1])
        self.conv3 = conv(self.batchNorm, 128, 256, kernel_size=5, stride=2, dropout=par.conv_dropout[2])
        self.conv3_1 = conv(self.batchNorm, 256, 256, kernel_size=3, stride=1, dropout=par.conv_dropout[3])
        self.conv4 = conv(self.batchNorm, 256, 512, kernel_size=3, stride=2, dropout=par.conv_dropout[4])
        self.conv4_1 = conv(self.batchNorm, 512, 512, kernel_size=3, stride=1, dropout=par.conv_dropout[5])
        self.conv5 = conv(self.batchNorm, 512, 512, kernel_size=3, stride=2, dropout=par.conv_dropout[6])
        self.conv5_1 = conv(self.batchNorm, 512, 512, kernel_size=3, stride=1, dropout=par.conv_dropout[7])
        self.conv6 = conv(self.batchNorm, 512, 1024, kernel_size=3, stride=2, dropout=par.conv_dropout[8])
        # Compute the shape based on diff image size
        __tmp = Variable(torch.zeros(1, 6, imsize1, imsize2))
        __tmp = self.encode_image(__tmp)

        # RNN
        self.rnn = nn.LSTM(
                input_size=int(np.prod(__tmp.size())),
                hidden_size=par.rnn_hidden_size,
                num_layers=par.rnn_num_layers,
                dropout=par.rnn_dropout_between,
                batch_first=True)
        self.rnn_drop_out = nn.Dropout(par.rnn_dropout_out)
        self.linear = nn.Linear(in_features=par.rnn_hidden_size, out_features=12)

        # Initilization
        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d) or isinstance(m, nn.Linear):
                kaiming_normal_(m.weight.data)
                if m.bias is not None:
                    m.bias.data.zero_()
            elif isinstance(m, nn.LSTM):
                # layer 1
                kaiming_normal_(m.weight_ih_l0)  # orthogonal_(m.weight_ih_l0)
                kaiming_normal_(m.weight_hh_l0)
                m.bias_ih_l0.data.zero_()
                m.bias_hh_l0.data.zero_()
                # Set forget gate bias to 1 (remember)
                n = m.bias_hh_l0.size(0)
                start, end = n // 4, n // 2
                m.bias_hh_l0.data[start:end].fill_(1.)

                # layer 2
                kaiming_normal_(m.weight_ih_l1)  # orthogonal_(m.weight_ih_l1)
                kaiming_normal_(m.weight_hh_l1)
                m.bias_ih_l1.data.zero_()
                m.bias_hh_l1.data.zero_()
                n = m.bias_hh_l1.size(0)
                start, end = n // 4, n // 2
                m.bias_hh_l1.data[start:end].fill_(1.)

            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()

    def forward(self, x, lstm_init_state=None):
        # x: (batch, seq_len, channel, width, height)
        # stack_image
        x = torch.cat((x[:, :-1], x[:, 1:]), dim=2)
        batch_size = x.size(0)
        seq_len = x.size(1)
        # CNN
        x = x.view(batch_size * seq_len, x.size(2), x.size(3), x.size(4))
        x = self.encode_image(x)
        x = x.view(batch_size, seq_len, -1)

        # lstm_init_state has the dimension of (# batch, 2 (hidden/cell), lstm layers, lstm hidden size)
        if lstm_init_state is not None:
            hidden_state = lstm_init_state[:, 0, :, :].permute(1, 0, 2).contiguous()
            cell_state = lstm_init_state[:, 1, :, :].permute(1, 0, 2).contiguous()
            lstm_init_state = (hidden_state, cell_state,)

        # RNN
        # lstm_state is (hidden state, cell state,)
        # each hidden/cell state has the shape (lstm layers, batch size, lstm hidden size)
        out, lstm_state = self.rnn(x, lstm_init_state)
        out = self.rnn_drop_out(out)
        out = self.linear(out)

        # rearrange the shape back to (# batch, 2 (hidden/cell), lstm layers, lstm hidden size)
        lstm_state = torch.stack(lstm_state, dim=0)
        lstm_state = lstm_state.permute(2, 0, 1, 3)

        return out, lstm_state

    def encode_image(self, x):
        out_conv2 = self.conv2(self.conv1(x))
        out_conv3 = self.conv3_1(self.conv3(out_conv2))
        out_conv4 = self.conv4_1(self.conv4(out_conv3))
        out_conv5 = self.conv5_1(self.conv5(out_conv4))
        out_conv6 = self.conv6(out_conv5)
        return out_conv6

    def weight_parameters(self):
        return [param for name, param in self.named_parameters() if 'weight' in name]

    def bias_parameters(self):
        return [param for name, param in self.named_parameters() if 'bias' in name]


class E2EVIO(nn.Module):
    def __init__(self):
        super(E2EVIO, self).__init__()

        self.vo_module = DeepVO(par.img_h, par.img_w, par.batch_norm)

        self.imu_noise_covar_diag_sqrt = nn.Parameter(torch.tensor(par.imu_noise_covar_diag_sqrt, dtype=torch.float32))
        if not par.train_imu_noise_covar:
            self.imu_noise_covar_diag_sqrt.requires_grad = False

        self.init_covar_diag_sqrt = nn.Parameter(torch.tensor(par.init_covar_diag_sqrt, dtype=torch.float32))
        if not par.train_init_covar:
            self.init_covar_diag_sqrt.requires_grad = False

        if par.fix_vo_weights:
            for param in self.vo_module.parameters():
                param.requires_grad = False

        self.ekf_module = IMUKalmanFilter()

    def forward(self, images, imu_data, prev_lstm_states, prev_pose, prev_state, T_imu_cam):
        vis_meas, lstm_states = self.vo_module.forward(images, lstm_init_state=prev_lstm_states)

        if par.vis_meas_covar_use_fixed:
            vis_meas_covar = torch.diag(torch.tensor(par.vis_meas_fixed_covar, dtype=torch.float32)). \
                repeat(vis_meas.shape[0],
                       vis_meas.shape[1], 1, 1).cuda()

        if not par.enable_ekf:
            return vis_meas, vis_meas_covar, lstm_states, None, None, None

        imu_noise_covar = torch.diag(self.imu_noise_covar_diag_sqrt * self.imu_noise_covar_diag_sqrt +
                                     par.imu_noise_covar_diag_eps)
        init_covar = torch.diag(self.init_covar_diag_sqrt * self.init_covar_diag_sqrt +
                                par.init_covar_diag_eps).repeat(vis_meas.shape[0], 1, 1)

        # vis model outputs positions first
        poses, ekf_states, ekf_covars = self.ekf_module.forward(imu_data, imu_noise_covar,
                                                                prev_pose, prev_state, init_covar,
                                                                vis_meas, vis_meas_covar, T_imu_cam)

        return vis_meas, vis_meas_covar, lstm_states, poses, ekf_states, ekf_covars
