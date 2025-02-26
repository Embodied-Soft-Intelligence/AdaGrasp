# ------------------------------------------------------------------------
# DINO
# Copyright (c) 2022 IDEA. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# DN-DETR
# Copyright (c) 2022 IDEA. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]


import torch
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)
# from .DABDETR import sigmoid_focal_loss
from util import box_ops
import torch.nn.functional as F


def prepare_for_cdn(dn_args, training, num_queries, num_classes, hidden_dim, label_enc):

    if training:

        targets, dn_number, label_noise_ratio, box_noise_scale = dn_args
        # positive and negative dn queries

        dn_number = dn_number * 2

        known = [(torch.ones_like(t['labels'])).cuda() for t in targets]
        batch_size = len(known)

        known_num = [sum(k) for k in known]

        if int(max(known_num)) == 0:
            dn_number = 1
        else:
            if dn_number >= 100:

                dn_number = dn_number // (int(max(known_num) * 2))
            elif dn_number < 1:
                dn_number = 1
        if dn_number == 0:
            dn_number = 1


        unmask_bbox = unmask_label = torch.cat(known)
        labels = torch.cat([t['labels'] for t in targets])
        boxes = torch.cat([t['boxes'] for t in targets])
        level = torch.cat([t['levels'] for t in targets])
        width_es = torch.cat([t['width_es'] for t in targets])
        angles = torch.cat([t['angles'] for t in targets])
        batch_idx = torch.cat([torch.full_like(t['labels'].long(), i) for i, t in enumerate(targets)])
        known_indice = torch.nonzero(unmask_label + unmask_bbox)
        known_indice = known_indice.view(-1)
        known_indice = known_indice.repeat(2 * dn_number, 1).view(-1)
        known_labels = labels.repeat(2 * dn_number, 1).view(-1)
        known_bid = batch_idx.repeat(2 * dn_number, 1).view(-1)
        known_bboxs = boxes.repeat(2 * dn_number, 1)
        known_levels = level.repeat(2 * dn_number, 1).view(-1)
        known_width_es = width_es.repeat(2 * dn_number, 1).view(-1)
        known_angles = angles.repeat(2 * dn_number, 1).view(-1)
        known_labels_expaned = known_labels.clone()
        known_bbox_expand = known_bboxs.clone()
        known_levels_expand = known_levels.clone()
        known_width_es_expand = known_width_es.clone()
        known_angles_expand = known_angles.clone()
        # --------------------------------------------------------------------------------------------------------------

        if label_noise_ratio > 0:
            p = torch.rand_like(known_labels_expaned.float())
            chosen_indice = torch.nonzero(p < (label_noise_ratio * 0.5)).view(-1)  # half of bbox prob
            new_label = torch.randint_like(chosen_indice, 1, 23)  # randomly put a new one here
            known_labels_expaned.scatter_(0, chosen_indice, new_label)


        single_pad = int(max(known_num))

        pad_size = int(single_pad * 2 * dn_number)
        positive_idx = torch.tensor(range(len(boxes))).long().cuda().unsqueeze(0).repeat(dn_number, 1)
        positive_idx += (torch.tensor(range(dn_number)) * len(boxes) * 2).long().cuda().unsqueeze(1)
        positive_idx = positive_idx.flatten()
        negative_idx = positive_idx + len(boxes)

        if box_noise_scale > 0:
            known_bbox_ = torch.zeros_like(known_bboxs)
            known_bbox_[:, :2] = known_bboxs[:, :2] - known_bboxs[:, 2:] / 2
            known_bbox_[:, 2:] = known_bboxs[:, :2] + known_bboxs[:, 2:] / 2

            diff = torch.zeros_like(known_bboxs)
            diff[:, :2] = known_bboxs[:, 2:] / 2
            diff[:, 2:] = known_bboxs[:, 2:] / 2
            rand_sign = torch.randint_like(known_bboxs, low=0, high=2, dtype=torch.float32) * 2.0 - 1.0
            rand_part = torch.rand_like(known_bboxs)

            rand_part[negative_idx] += 1.0
            rand_part *= rand_sign
            known_bbox_ = known_bbox_ + torch.mul(rand_part,
                                                  diff).cuda() * box_noise_scale
            known_bbox_ = known_bbox_.clamp(min=0.0, max=1.0)
            known_bbox_expand[:, :2] = (known_bbox_[:, :2] + known_bbox_[:, 2:]) / 2
            known_bbox_expand[:, 2:] = known_bbox_[:, 2:] - known_bbox_[:, :2]


            known_end_width_ = torch.zeros_like(known_bboxs[:, :2])
            known_end_width_[:, 0] = known_bboxs[:, 0] - known_width_es / 2
            known_end_width_[:, 1] = known_bboxs[:, 0] + known_width_es / 2

            diff_width = torch.zeros_like(known_bboxs[:, :2])
            diff_width[:, 0] = known_width_es / 2
            diff_width[:, 1] = known_width_es / 2

            rand_sign_width = torch.randint_like(known_bboxs[:, :2], low=0, high=2, dtype=torch.float32) * 2.0 - 1.0
            rand_part_width = torch.rand_like(known_bboxs[:, :2])

            rand_part_width[negative_idx] += 1.0
            rand_part_width *= rand_sign_width
            known_end_width_ = known_end_width_ + torch.mul(rand_part_width,
                                                           diff_width).cuda() * box_noise_scale
            known_end_width_ = known_end_width_.clamp(min=0.0, max=1.0)
            known_width_es_expand = known_end_width_[:, 1] - known_end_width_[:, 0]
            known_angle_ = known_angles

            noise_angle = torch.zeros_like(known_angles)
            noise_sign_angle = torch.randint_like(noise_angle, low=0, high=2, dtype=torch.float32) * 2.0 - 1.0
            noise_part_angle = torch.rand_like(noise_angle)

            noise_part_angle[negative_idx] += 1.0
            noise_part_angle *= noise_sign_angle
            known_angles_ = known_angle_ + torch.mul(noise_part_angle, 0.125).cuda()

            known_angles_expand = torch.where(known_angles_ > 1, known_angles_ - 1, known_angles_)
            known_angles_expand = torch.where(known_angles_ < 0, known_angles_ + 1, known_angles_)

            known_levels_ = known_levels
            noise_level = torch.zeros_like(known_levels)
            noise_sign_level = torch.randint_like(noise_level, low=0, high=2, dtype=torch.float32) * 2.0 - 1.0
            noise_part_level = torch.rand_like(noise_level)

            noise_part_level[negative_idx] += 1.0
            noise_part_level *= noise_sign_level
            known_levels_ = known_levels_ + torch.mul(noise_part_level, 0.125).cuda()
            known_levels_expand = torch.where(known_levels_ > 1, known_levels_ - 1, known_levels_)
            known_levels_expand = torch.where(known_levels_ < 0, known_levels_ + 1, known_levels_)



        m = known_labels_expaned.long().to('cuda')

        input_label_embed = label_enc(m)

        input_bbox_embed = inverse_sigmoid(known_bbox_expand)
        input_level_embed = inverse_sigmoid(known_levels_expand.unsqueeze(1))
        input_width_embed = inverse_sigmoid(known_width_es_expand.unsqueeze(1))
        input_angle_embed = inverse_sigmoid(known_angles_expand.unsqueeze(1))
        # !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        # input_angle_embed = known_angle_.unsqueeze(1)


        # [pad_size, 256]
        padding_label = torch.zeros(pad_size, hidden_dim).cuda()
        # [pad_size, 4]
        padding_bbox = torch.zeros(pad_size, 4).cuda()
        input_query_label = padding_label.repeat(batch_size, 1, 1)
        input_query_bbox = padding_bbox.repeat(batch_size, 1, 1)

        padding_level = torch.zeros(pad_size, 1).cuda()
        input_query_level = padding_level.repeat(batch_size, 1, 1)

        padding_width = torch.zeros(pad_size, 1).cuda()
        input_query_width = padding_width.repeat(batch_size, 1, 1)

        padding_angle = torch.zeros(pad_size, 1).cuda()
        input_query_angle = padding_angle.repeat(batch_size, 1, 1)

        map_known_indice = torch.tensor([]).to('cuda')
        if len(known_num):
            map_known_indice = torch.cat([torch.tensor(range(num)) for num in known_num])
            map_known_indice = torch.cat([map_known_indice + single_pad * i for i in range(2 * dn_number)]).long()

        if len(known_bid):
            input_query_label[(known_bid.long(), map_known_indice)] = input_label_embed

            input_query_bbox[(known_bid.long(), map_known_indice)] = input_bbox_embed

            input_query_level[(known_bid.long(), map_known_indice)] = input_level_embed

            input_query_width[(known_bid.long(), map_known_indice)] = input_width_embed

            input_query_angle[(known_bid.long(), map_known_indice)] = input_angle_embed

        tgt_size = pad_size + num_queries

        attn_mask = torch.ones(tgt_size, tgt_size).to('cuda') < 0

        # match query cannot see the reconstruct
        attn_mask[pad_size:, :pad_size] = True

        # reconstruct cannot see each other
        for i in range(dn_number):

            if i == 0:
                attn_mask[single_pad * 2 * i:single_pad * 2 * (i + 1), single_pad * 2 * (i + 1):pad_size] = True
            if i == dn_number - 1:
                attn_mask[single_pad * 2 * i:single_pad * 2 * (i + 1), :single_pad * i * 2] = True
            else:
                attn_mask[single_pad * 2 * i:single_pad * 2 * (i + 1), single_pad * 2 * (i + 1):pad_size] = True
                attn_mask[single_pad * 2 * i:single_pad * 2 * (i + 1), :single_pad * 2 * i] = True

        dn_meta = {
            'pad_size': pad_size,
            'num_dn_group': dn_number,  
        }
    else:

        input_query_label = None
        input_query_bbox = None
        input_query_level = None
        attn_mask = None
        dn_meta = None
        input_query_width = None
        input_query_angle = None
    return input_query_label, input_query_bbox, attn_mask, dn_meta, input_query_width, input_query_angle, input_query_level


def dn_post_process(outputs_class, outputs_coord, dn_meta, aux_loss, _set_aux_loss):
    """
        post process of dn after output from the transformer
        put the dn part in the dn_meta
    """
    if dn_meta and dn_meta['pad_size'] > 0:
        output_known_class = outputs_class[:, :, :dn_meta['pad_size'], :]
        output_known_coord = outputs_coord[:, :, :dn_meta['pad_size'], :]
        outputs_class = outputs_class[:, :, dn_meta['pad_size']:, :]
        outputs_coord = outputs_coord[:, :, dn_meta['pad_size']:, :]
        out = {'pred_logits': output_known_class[-1], 'pred_boxes': output_known_coord[-1]}
        if aux_loss:
            out['aux_outputs'] = _set_aux_loss(output_known_class, output_known_coord)
        dn_meta['output_known_lbs_bboxes'] = out
    return outputs_class, outputs_coord
