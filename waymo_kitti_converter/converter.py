import os
import numpy as np
import cv2
import matplotlib.pyplot as plt
import tensorflow as tf
import tensorflow.compat.v1 as tf

import tqdm
from multiprocessing import Pool
from os.path import join, isdir, isfile
import argparse
from glob import glob

from waymo_open_dataset.utils.frame_utils import parse_range_image_and_camera_projection
from waymo_open_dataset import dataset_pb2 as open_dataset
from waymo_open_dataset import dataset_pb2
from waymo_open_dataset.utils import range_image_utils
from waymo_open_dataset.utils import transform_utils

import math
import sys
import numpy
numpy.set_printoptions(threshold=sys.maxsize)

# from waymo_open_dataset.utils import  frame_utils

from matplotlib import patches
# from waymo_open_dataset import label_pb2
# from waymo_open_dataset.camera.ops import py_camera_model_ops
# from waymo_open_dataset.metrics.ops import py_metrics_ops
# from waymo_open_dataset.metrics.python import config_util_py as config_util
# from waymo_open_dataset.protos import breakdown_pb2
# from waymo_open_dataset.protos import metrics_pb2
# from waymo_open_dataset.protos import submission_pb2
# from waymo_open_dataset.utils import box_utils

import itertools
import immutabledict

if not tf.executing_eagerly():
  tf.compat.v1.enable_eager_execution()

from waymo_open_dataset.utils import camera_segmentation_utils

tf.enable_eager_execution()

             
# Abbreviations:
# WOD: Waymo Open Dataset
# FOV: field of view
# SDC: self-driving car
# 3dbox: 3D bounding box

# Some 3D bounding boxes do not contain any points
# This switch, when set True, filters these boxes
# It is safe to filter these boxes because they are not counted towards evaluation anyway
filter_empty_3dboxes = False


# There is no bounding box annotations in the No Label Zone (NLZ)
# if set True, points in the NLZ are filtered
filter_no_label_zone_points = True


# Only bounding boxes of certain classes are converted
# Note: Waymo Open Dataset evaluates for ALL_NS, including only 'VEHICLE', 'PEDESTRIAN', 'CYCLIST'
selected_waymo_classes = [
    # 'UNKNOWN',
    'VEHICLE',
    # 'PEDESTRIAN',
    # 'SIGN',
    # 'CYCLIST'
]


# Only data collected in specific locations will be converted
# If set None, this filter is disabled (all data will thus be converted)
# Available options: location_sf (main dataset)
selected_waymo_locations = None

# Save track id
save_track_id = True

# DATA_PATH = '/media/alex/Seagate Expansion Drive/waymo_open_dataset/domain_adaptation_training_labelled(partial)'
# KITTI_PATH = '/home/alex/github/waymo_to_kitti_converter/tools/pose'


class WaymoToKITTI(object):

    def __init__(self, load_dir, save_dir, prefix, num_proc):
        # turn on eager execution for older tensorflow versions
        if int(tf.__version__.split('.')[0]) < 2:
            tf.enable_eager_execution()

        self.lidar_list = ['_FRONT', '_FRONT_RIGHT', '_FRONT_LEFT', '_SIDE_RIGHT', '_SIDE_LEFT']
        self.type_list = ['UNKNOWN', 'VEHICLE', 'PEDESTRIAN', 'SIGN', 'CYCLIST']
        output_path = '/content/'
        self.class_list =['Undefined','Ego_vehicle','Car','Truck','Bus','Other_large_vehicle','Bicycle',
             'Motorcycle','Trailer','Pedestrian','Cyclist','Motorcyclist','Bird','Ground_animal',
             'Construction_cone_pole','Pole','Pedestrian_object','Sign','Traffic_light','Building',
             'Road','Lane_marker','Road_marker','Sidewalk','Vegetation','Sky','Ground','Dynamic','Static']
        self.class_color_list =[[0, 0, 0],[102, 102, 102],[0, 0, 142],[0, 0, 70],[0, 60, 100],[61, 133, 198],[119, 11, 32],
                                [0, 0, 230],[111, 168, 220],[220, 20, 60],[255, 0, 0],[180, 0, 0],[127, 96, 0],[91, 15, 0],
                                [230, 145, 56],[153, 153, 153],[234, 153, 153],[246, 178, 107],[250, 170, 30],[70, 70, 70],
                                [128, 64, 128],[234, 209, 220],[217, 210, 233],[244, 35, 232],[107, 142, 35],[70, 130, 180],[102, 102, 102],[102, 102, 102],[102, 102, 102]]

        self.waymo_to_kitti_class_map = {
            'UNKNOWN': 'DontCare',
            'PEDESTRIAN': 'Pedestrian',
            'VEHICLE': 'Car',
            'CYCLIST': 'Cyclist',
            'SIGN': 'Sign'  # not in kitti
        }

        self.load_dir = load_dir
        self.save_dir = save_dir
        self.prefix   = prefix
        self.num_proc = int(num_proc)

        self.tfrecord_pathnames = sorted(glob(join(self.load_dir, '*.tfrecord')))

        # self.label_save_dir       = self.save_dir + '/label_'
        self.label_save_dir       = self.save_dir + '/vkitti_1.3.1_motgt'
        self.label_all_save_dir   = self.save_dir + '/label_all'
        # self.image_save_dir       = self.save_dir + '/image_'
        self.image_save_dir       = self.save_dir + '/vkitti_1.3.1_rgb'
        self.calib_save_dir       = self.save_dir + '/calib'
        self.point_cloud_save_dir = self.save_dir + '/velodyne'
        # self.pose_save_dir        = self.save_dir + '/pose'
        self.pose_save_dir        = self.save_dir + '/vkitti_1.3.1_extrinsicsgt'        
        self.pvp_save_dir         = self.save_dir + '/vkitti_1.3.1_scenegt'
        self.create_folder()

    def convert(self):
        print("start converting ...")
        with Pool(self.num_proc) as p:
            r = list(tqdm.tqdm(p.imap(self.convert_one, range(len(self))), total=len(self)))
        print("\nfinished ...")

    def _pad_to_common_shape(self,label):
        return np.pad(label, [[1280 - label.shape[0], 0], [0, 0], [0, 0]])

    def convert_one(self, file_idx):
        pathname = self.tfrecord_pathnames[file_idx]
        dataset = tf.data.TFRecordDataset(pathname, compression_type='')
        
        # file name with extension
        file_name = os.path.basename(pathname)

        # file name without extension
        segment_name = os.path.splitext(file_name)[0]
        print(segment_name)
        print(file_idx)
        # Avoid repeat object id in the output text file
        frame_obj_id = []
        # Avoid repeat segmentation class in the output text file
        segment_class = []

        # if output_path is not None:
        # cur_det_file = output_path + ('%s_clone_scenegt_rgb_encoding.txt' % segment_name)
        # if os.path.exists(cur_det_file):
        #     os.remove(cur_det_file)

        for frame_idx, data in enumerate(dataset):

            frame = open_dataset.Frame()
            frame.ParseFromString(bytearray(data.numpy()))
            if selected_waymo_locations is not None and frame.context.stats.location not in selected_waymo_locations:
                continue

            # Only output the labels for the frame has segmentation labels
            if not frame.images[0].camera_segmentation_label.panoptic_label: continue

            # save images
            self.save_image(frame, file_idx, frame_idx)

            # parse calibration files
            self.save_calib(frame, file_idx, frame_idx)

            # # parse point clouds
            # self.save_lidar(frame, file_idx, frame_idx)

            # parse 2D Panoramic Video Panoptic Segmentation files
            global_id_label_concat = self.save_2D_semantic(frame, file_idx, frame_idx, frame_obj_id, segment_class)
            
            # parse label files
            self.save_label(frame, file_idx, frame_idx, global_id_label_concat)

            # parse pose files
            self.save_pose(frame, file_idx, frame_idx)


        with open(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + '.txt', 'r+') as f:
            lines = f.readlines()
        with open(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + '.txt', 'w+') as f:
            header = lines[:29]
            objects = lines[29:]
            temp_dict = { obj : int(obj.split(":")[1].split(" ")[0]) for obj in objects }
            sortedDict = sorted(temp_dict.items(), key=lambda x:x[1])
            sortedDict = [item[0] for item in sortedDict]
            header.extend(sortedDict)
            for string in header: 
                print(string[:-1], file=f)
            f.close()
        print(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + ' '+" Created Successfully")

    def __len__(self):
        return len(self.tfrecord_pathnames)

    def save_image(self, frame, file_idx, frame_idx):
        """ parse and save the images in png format
                :param frame: open dataset frame proto
                :param file_idx: the current file number
                :param frame_idx: the current frame number
                :return:
        """
        for img in frame.images:
            # frame.images[0] represent the front camera
            # img_path = self.image_save_dir + str(img.name - 1) + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.png'
            img_folder_path = self.image_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '/clone/'

            if not isdir(img_folder_path):
                os.makedirs(img_folder_path)

            img_path = img_folder_path + str(frame_idx).zfill(5) + '.png'

            img = cv2.imdecode(np.frombuffer(img.image, np.uint8), cv2.IMREAD_COLOR)
            rgb_img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            rgb_img = rgb_img[:,:,:3] # remove alpha channel
            plt.imsave(img_path, rgb_img, format='png')
            break # output first camera only

    def save_calib(self, frame, file_idx, frame_idx):
        """ parse and save the calibration data
                :param frame: open dataset frame proto
                :param file_idx: the current file number
                :param frame_idx: the current frame number
                :return:
        """
        # kitti:
        #   bbox in reference camera frame (right-down-front)
        #       image_x_coord = Px * R0_rect * R0_rot * bbox_coord
        #   lidar points in lidar frame (front-right-up)
        #       image_x_coord = Px * R0_rect * Tr_velo_to_cam * lidar_coord
        #   note:   R0_rot is caused by bbox rotation
        #           Tr_velo_to_cam projects lidar points to cam_0 frame
        # waymo:
        #   bbox in vehicle frame, hence, use a virtual reference frame
        #   since waymo camera uses frame front-left-up, the virtual reference frame (right-down-front) is
        #   built on a transformed front camera frame, name this transform T_front_cam_to_ref
        #   and there is no rectified camera frame
        #       image_x_coord = intrinsics_x * Tr_front_cam_to_cam_x * inv(T_front_cam_to_ref) * R0_rot * bbox_coord(now in ref frame)
        #   lidar points in vehicle frame
        #       image_x_coord = intrinsics_x * Tr_front_cam_to_cam_x * inv(T_front_cam_to_ref) * T_front_cam_to_ref * Tr_velo_to_front_cam * lidar_coord
        # hence, waymo -> kitti:
        #   set Tr_velo_to_cam = T_front_cam_to_ref * Tr_vehicle_to_front_cam = T_front_cam_to_ref * inv(Tr_front_cam_to_vehicle)
        #       as vehicle and lidar use the same frame after fusion
        #   set R0_rect = identity
        #   set P2 = front_cam_intrinsics * Tr_waymo_to_conv * Tr_front_cam_to_front_cam * inv(T_front_cam_to_ref)
        #   note: front cam is cam_0 in kitti, whereas has name = 1 in waymo
        #   note: waymo camera has a front-left-up frame,
        #       instead of the conventional right-down-front frame
        #       Tr_waymo_to_conv is used to offset this difference. However, Tr_waymo_to_conv is the same as
        #       T_front_cam_to_ref, hence,
        #   set P2 = front_cam_intrinsics

        calib_context = ''

        # front-left-up -> right-down-front
        # T_front_cam_to_ref = np.array([
        #     [0.0, -1.0, 0.0],
        #     [-1.0, 0.0, 0.0],
        #     [0.0, 0.0, 1.0]
        # ])
        T_front_cam_to_ref = np.array([
            [0.0, -1.0, 0.0],
            [0.0, 0.0, -1.0],
            [1.0, 0.0, 0.0]
        ])
        # T_ref_to_front_cam = np.array([
        #     [0.0, 0.0, 1.0],
        #     [-1.0, 0.0, 0.0],
        #     [0.0, -1.0, 0.0]
        # ])

        # print('context\n',frame.context)

        for camera in frame.context.camera_calibrations:
            if camera.name == 1:  # FRONT = 1, see dataset.proto for details
                T_front_cam_to_vehicle = np.array(camera.extrinsic.transform).reshape(4, 4)
                # print('T_front_cam_to_vehicle\n', T_front_cam_to_vehicle)
                T_vehicle_to_front_cam = np.linalg.inv(T_front_cam_to_vehicle)

                front_cam_intrinsic = np.zeros((3, 4))
                front_cam_intrinsic[0, 0] = camera.intrinsic[0]
                front_cam_intrinsic[1, 1] = camera.intrinsic[1]
                front_cam_intrinsic[0, 2] = camera.intrinsic[2]
                front_cam_intrinsic[1, 2] = camera.intrinsic[3]
                front_cam_intrinsic[2, 2] = 1

                break

        # print('front_cam_intrinsic\n', front_cam_intrinsic)

        self.T_front_cam_to_ref = T_front_cam_to_ref.copy()
        self.T_vehicle_to_front_cam = T_vehicle_to_front_cam.copy()

        identity_3x4 = np.eye(4)[:3, :]

        # although waymo has 5 cameras, for compatibility, we produces 4 P
        # for i in range():
        for i in range(4):
            if i == 2:
                # note: front camera is labeled camera 2 (kitti) or camera 0 (waymo)
                #   other Px are given dummy values. this is to ensure compatibility. They are seldom used anyway.
                # tmp = cart_to_homo(np.linalg.inv(T_front_cam_to_ref))
                # print(front_cam_intrinsic.shape, tmp.shape)
                # P2 = np.matmul(front_cam_intrinsic, tmp).reshape(12)
                P2 = front_cam_intrinsic.reshape(12)
                calib_context += "P2: " + " ".join(['{}'.format(i) for i in P2]) + '\n'
            else:
                calib_context += "P" + str(i) + ": " + " ".join(['{}'.format(i) for i in identity_3x4.reshape(12)]) + '\n'

        calib_context += "R0_rect" + ": " + " ".join(['{}'.format(i) for i in np.eye(3).astype(np.float32).flatten()]) + '\n'

        Tr_velo_to_cam = self.cart_to_homo(T_front_cam_to_ref) @ np.linalg.inv(T_front_cam_to_vehicle)
        # print('T_front_cam_to_vehicle\n', T_front_cam_to_vehicle)
        # print('np.linalg.inv(T_front_cam_to_vehicle)\n', np.linalg.inv(T_front_cam_to_vehicle))
        # print('cart_to_homo(T_front_cam_to_ref)\n', cart_to_homo(T_front_cam_to_ref))
        # print('Tr_velo_to_cam\n',Tr_velo_to_cam)
        calib_context += "Tr_velo_to_cam" + ": " + " ".join(['{}'.format(i) for i in Tr_velo_to_cam[:3, :].reshape(12)]) + '\n'

        with open(self.calib_save_dir + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt', 'w+') as fp_calib:
            fp_calib.write(calib_context)

    def save_lidar(self, frame, file_idx, frame_idx):
        """ parse and save the lidar data in psd format
                :param frame: open dataset frame proto
                :param file_idx: the current file number
                :param frame_idx: the current frame number
                :return:
                """
        # range_images, camera_projections, range_image_top_pose = parse_range_image_and_camera_projection(frame)
        range_images, camera_projections, _, range_image_top_pose = (parse_range_image_and_camera_projection(frame))

        points_0, cp_points_0, intensity_0 = self.convert_range_image_to_point_cloud(
            frame,
            range_images,
            camera_projections,
            range_image_top_pose,
            ri_index=0
        )
        points_0 = np.concatenate(points_0, axis=0)
        intensity_0 = np.concatenate(intensity_0, axis=0)

        points_1, cp_points_1, intensity_1 = self.convert_range_image_to_point_cloud(
            frame,
            range_images,
            camera_projections,
            range_image_top_pose,
            ri_index=1
        )
        points_1 = np.concatenate(points_1, axis=0)
        intensity_1 = np.concatenate(intensity_1, axis=0)

        points = np.concatenate([points_0, points_1], axis=0)
        # print('points_0', points_0.shape, 'points_1', points_1.shape, 'points', points.shape)
        intensity = np.concatenate([intensity_0, intensity_1], axis=0)
        # points = points_1
        # intensity = intensity_1

        # reference frame:
        # front-left-up (waymo) -> right-down-front(kitti)
        # lidar frame:
        # ?-?-up (waymo) -> front-right-up (kitti)

        # print('bef\n', points)
        # print('bef\n', points.dtype)
        # points = np.transpose(points)  # (n, 3) -> (3, n)
        # tf = np.array([
        #     [0.0, -1.0,  0.0],
        #     [0.0,  0.0, -1.0],
        #     [1.0,  0.0,  0.0]
        # ])
        # points = np.matmul(tf, points)
        # points = np.transpose(points)  # (3, n) -> (n, 3)
        # print('aft\n', points)
        # print('aft\n', points.dtype)

        # concatenate x,y,z and intensity
        point_cloud = np.column_stack((points, intensity))


        # print(point_cloud.shape)

        # save
        pc_path = self.point_cloud_save_dir + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.bin'
        point_cloud.astype(np.float32).tofile(pc_path)  # note: must save as float32, otherwise loading errors

    def image_resize(self, image, width = None, height = None, inter = cv2.INTER_AREA):
        # initialize the dimensions of the image to be resized and
        # grab the image size
        dim = None
        (h, w) = image.shape[:2]

        # if both the width and height are None, then return the
        # original image
        if width is None and height is None:
            return image

        # check to see if the width is None
        if width is None:
            # calculate the ratio of the height and construct the
            # dimensions
            r = height / float(h)
            dim = (int(w * r), height)

        # otherwise, the height is None
        else:
            # calculate the ratio of the width and construct the
            # dimensions
            r = width / float(w)
            dim = (width, int(h * r))

        # resize the image
        resized = cv2.resize(image, dim, interpolation = inter)

        # return the resized image
        return resized

    def save_projected_lidar_labels(self, camera_image, frame):
        """Save pre-projected 3D laser labels as cropped images."""
        label_list = []
        label_id_list = []
        bb_boxes_list = []

        for projected_labels in frame.projected_lidar_labels:
            # Ignore camera labels that do not correspond to this camera.
            if projected_labels.name != camera_image.name:
                continue

            # Iterate over the individual labels.
            for label in projected_labels.labels:
                if label.type!= 1: continue # 'VEHICLE'

                img = tf.image.decode_jpeg(camera_image.image).numpy()# tensor to numpy
                im_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                width, height, channel = im_rgb.shape

                # print(im_rgb.shape) # Print image shape
                
                x_1 = round(label.box.center_x - label.box.length / 2)
                y_1 = round(label.box.center_y - label.box.width / 2)
                x_2 = round(label.box.center_x + label.box.length / 2 )
                y_2 = round(label.box.center_y + label.box.width / 2 )

                # print(x_1,y_1,x_2,y_2)

                # # Turn off this line while output, otherwise green surrounding box
                # im_rgb_rectangle = cv2.rectangle(im_rgb,(x_1,y_1),(x_2,y_2),(0,255,0),2)
                # cv2_imshow(im_rgb_rectangle)

                # Cropping an image
                cropped_image = im_rgb[y_1:y_2, x_1:x_2]
                dim = (int(height/10), int(width/10))
                    
                # resize image
                # cropped_image = cv2.resize(cropped_image, dim, interpolation = cv2.INTER_AREA)
                cropped_image = self.image_resize(cropped_image, height = dim[0])

                ## Display cropped image
                # cv2_imshow(cropped_image)
                # print(type(cropped_image)) 

                label_list.append(cropped_image)
                label_id_list.append(label.id)
                bb_boxes_list.append(np.array([x_1, y_1, x_2, y_2]))
                # print(len(label_list))
        return label_list, label_id_list, bb_boxes_list

    def save_camera_2d_image(self, camera_image, frame, camera_labels, cmap=None ):
        """Save a camera image and the given camera labels as cropped images."""
        label_list = []
        bb_boxes_list = []
        # Draw the camera labels.
        for camera_labels in frame.camera_labels:
            # Ignore camera labels that do not correspond to this camera.
            if camera_labels.name != camera_image.name:
                continue

            # Iterate over the individual labels.
            for label in camera_labels.labels:
                if label.type!= 1: continue # 'VEHICLE'
                
                # print(label)
                
                img = tf.image.decode_jpeg(camera_image.image).numpy()# tensor to numpy
                im_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                width, height, channel = im_rgb.shape

                # print(im_rgb.shape) # Print image shape
                
                x_1 = round(label.box.center_x - label.box.length / 2)
                y_1 = round(label.box.center_y - label.box.width / 2)
                x_2 = round(label.box.center_x + label.box.length / 2 )
                y_2 = round(label.box.center_y + label.box.width / 2 )

                # print(x_1,y_1,x_2,y_2)

                # # Turn off this line while output, otherwise green surrounding box
                # im_rgb_rectangle = cv2.rectangle(im_rgb,(x_1,y_1),(x_2,y_2),(0,255,0),2)
                # cv2_imshow(im_rgb_rectangle)

                # Cropping an image
                cropped_image = im_rgb[y_1:y_2, x_1:x_2]
                dim = (int(height/10), int(width/10))
                    
                # resize image
                # cropped_image = cv2.resize(cropped_image, dim, interpolation = cv2.INTER_AREA)
                cropped_image = self.image_resize(cropped_image, height = dim[0])

                # # Display cropped image
                # cv2_imshow(cropped_image)
                # # print(type(cropped_image)) 

                label_list.append(cropped_image)
                # print(len(label_list))
                # bb_boxes_list.append(np.array([x_1, y_1, x_2, y_2]))
                bb_boxes_list.append([x_1, y_1, x_2, y_2])

        return label_list, bb_boxes_list

    def get_iou(self, ground_truth, pred):
        # coordinates of the area of intersection.
        ix1 = np.maximum(ground_truth[0], pred[0])
        iy1 = np.maximum(ground_truth[1], pred[1])
        ix2 = np.minimum(ground_truth[2], pred[2])
        iy2 = np.minimum(ground_truth[3], pred[3])
        
        # Intersection height and width.
        i_height = np.maximum(iy2 - iy1 + 1, np.array(0.))
        i_width = np.maximum(ix2 - ix1 + 1, np.array(0.))
        
        area_of_intersection = i_height * i_width
        
        # Ground Truth dimensions.
        gt_height = ground_truth[3] - ground_truth[1] + 1
        gt_width = ground_truth[2] - ground_truth[0] + 1
        
        # Prediction dimensions.
        pd_height = pred[3] - pred[1] + 1
        pd_width = pred[2] - pred[0] + 1
        
        area_of_union = gt_height * gt_width + pd_height * pd_width - area_of_intersection
        
        iou = area_of_intersection / area_of_union
        
        return iou

    def save_label(self, frame, file_idx, frame_idx, global_id_label_concat):
        """ parse and save the label data in .txt format
                :param frame: open dataset frame proto
                :param file_idx: the current file number
                :param frame_idx: the current frame number
                :param global_id_label_concat: Given this numpy segmentation map annoated by global id to locate global id for each 2D bounding box
                :return:
                """
        # fp_label_all = open(self.label_all_save_dir + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt', 'w+')
        
        id_to_camera_bbox = dict()
        for image in frame.images:
            
            lidar_list, label_id_list, camera_list, lidar_bb, camera_bb = [],[],[],[],[]
            lidar_list, label_id_list, lidar_bb = self.save_projected_lidar_labels(image, frame)
            # print(image.name,': lidar_list:',len(lidar_list))
            camera_list, camera_bb = self.save_camera_2d_image(image, frame, frame.camera_labels)
            # print(image.name,': camera_list:',len(camera_list))
            
            if len(lidar_list) and len(camera_list)>0:
                for i,lidar in enumerate(lidar_list):
                    rank_list = []
                    for j,camera_patch in enumerate(camera_list):                        
                        IoU = self.get_iou(camera_bb[j], lidar_bb[i])
                        rank_list.append(IoU)

                    # print(round(rank_list[np.argmax(rank_list)],2),rank_list)
                    # if round(rank_list[np.argmax(rank_list)]) > 0.3:
                    if max(rank_list)>0.4:

                        # cv2_imshow(lidar)
                        # cv2_imshow(camera_list[np.argmax(rank_list)])
                        id_to_camera_bbox[label_id_list[i]] = camera_bb[np.argmax(rank_list)]

            break # output the front camera only
        
        # print(id_to_camera_bbox)

        # preprocess bounding box data
        # id_to_bbox = dict()
        id_to_name = dict()
        for labels in frame.projected_lidar_labels:
            name = labels.name
            for label in labels.labels:
                # # waymo: bounding box origin is at the center
                # # TODO: need a workaround as bbox may not belong to front cam
                # bbox = [label.box.center_x - label.box.length / 2, label.box.center_y - label.box.width / 2,
                #         label.box.center_x + label.box.length / 2, label.box.center_y + label.box.width / 2]
                # id_to_bbox[label.id] = bbox
                id_to_name[label.id] = name - 1
        
        # print(id_to_name)

        # file_name = self.label_save_dir + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt'
        file_name = self.label_save_dir + '/' + str(file_idx).zfill(4) + self.prefix + '_clone' + '.txt'

        if not isfile(file_name):
            with open(file_name, 'a') as f:
                f.write('frame tid label truncated occluded alpha l t r b w3d h3d l3d x3d y3d z3d ry rx rz truncr occupr orig_label moving model color\n')
                f.close()

        # print([i.type for i in frame.laser_labels])
        for obj in frame.laser_labels:

            # calculate bounding box
            bounding_box = None
            name = None
            id = obj.id
            for lidar in self.lidar_list:
                # if id + lidar in id_to_bbox:
                if id + lidar in id_to_camera_bbox:
                    bounding_box = id_to_camera_bbox.get(id + lidar)
                    name = str(id_to_name.get(id + lidar))
                    break
            # print(bounding_box)
            # TODO: temp fix
            if bounding_box == None or name == None:
                name = '00'
                bounding_box = (0, 0, 0, 0)
            # print(id, name, type(name))
            if name != '0': continue  # output first camera only; Ignore other cameras

            my_type = self.type_list[obj.type]

            if my_type not in selected_waymo_classes:
                continue

            if filter_empty_3dboxes and obj.num_lidar_points_in_box < 1:
                continue

            # from waymo_open_dataset.utils.box_utils import compute_num_points_in_box_3d
            # print('annot:', obj.num_lidar_points_in_box)
            # num_points_in_gt_waymo = compute_num_points_in_box_3d(
            #     tf.convert_to_tensor(self.pc.astype(np.float32), dtype=tf.float32),
            #     tf.convert_to_tensor(np.array([[obj.box.center_x, obj.box.center_y, obj.box.center_z,  obj.box.length,obj.box.width,  obj.box.height,obj.box.heading]]).astype(np.float32), dtype=tf.float32))
            # print('actual:', num_points_in_gt_waymo.numpy())

            # visualizer
            # [261   56   24   15   46  254   24  824  146   26    5   13   30   45
            #  60  184  347  222 1774    2   46]

            # converter
            # 264, 59, 24, 16, 51, 268, 24, 847, 149, 28, 6, 13, 30, 45, \
            # 64, 192, 353, 229, 1848, 2, 48
            
            # print(global_id_label_concat.shape)
            # print(bounding_box)
            
            # tid = global_id_label_concat[int((bounding_box[0]+bounding_box[2])/2)][int((bounding_box[1]+bounding_box[3])/2)][0]
            
            # print(global_id_label_concat[int((bounding_box[1]+bounding_box[3])/2)][int((bounding_box[0]+bounding_box[2])/2)])

            tid = global_id_label_concat[int((bounding_box[1]+bounding_box[3])/2)][int((bounding_box[0]+bounding_box[2])/2)][0]
            if tid==0: 
                continue

            my_type = self.waymo_to_kitti_class_map[my_type]

            # length: along the longer axis that is perpendicular to gravity direction
            # width: along the shorter axis  that is perpendicular to gravity direction
            # height: along the gravity direction
            # the same for waymo and kitti
            height = obj.box.height  # up/down
            width = obj.box.width  # left/right
            length = obj.box.length  # front/back

            # waymo: bbox label in lidar/vehicle frame. kitti: bbox label in reference image frame
            # however, kitti uses bottom center as the box origin, whereas waymo uses the true center
            x = obj.box.center_x
            y = obj.box.center_y
            z = obj.box.center_z - height / 2

            # print('bef', x,y,z)

            # project bounding box to the virtual reference frame
            pt_ref = self.cart_to_homo(self.T_front_cam_to_ref) @ self.T_vehicle_to_front_cam @ np.array([x,y,z,1]).reshape((4,1))
            x, y, z, _ = pt_ref.flatten().tolist()

            # print('aft', x,y,z)

            # x, y, z correspond to l, w, h (waymo) -> l, h, w (kitti)
            # length, width, height = length, height, width

            # front-left-up (waymo) -> right-down-front(kitti)
            # bbox origin at volumetric center (waymo) -> bottom center (kitti)
            # x, y, z = -waymo_y, -waymo_z + height / 2, waymo_x

            # rotation: +x around y-axis (kitti) -> +x around y-axis (waymo)
            #           right-down-front            front-left-up
            # note: the "rotation_y" is kept as the name of the rotation variable for compatibility
            # it is, in fact, rotation around positive z
            rotation_y = -obj.box.heading - np.pi / 2

            # track id
            track_id = obj.id

            # not available
            truncated = 0
            occluded = 0

            # alpha:
            # we set alpha to the default -10, the same as nuscenes to kitti tool
            # contribution is welcome
            alpha = -10

            # save the labels
            # print(frame_idx,tid,my_type,truncated,occluded,alpha,bounding_box,height,width,length,x,y,z,rotation_y)
            line = str(frame_idx) +' {}'.format(tid) + ' ' + my_type + ' {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {}\n'.format(round(truncated, 2),
                                                                                                                            occluded,
                                                                                                                            round(alpha, 2),
                                                                                                                            round(bounding_box[0], 2),
                                                                                                                            round(bounding_box[1], 2),
                                                                                                                            round(bounding_box[2], 2),
                                                                                                                            round(bounding_box[3], 2),
                                                                                                                            round(height, 2),
                                                                                                                            round(width, 2),
                                                                                                                            round(length, 2),
                                                                                                                            round(x, 2),
                                                                                                                            round(y, 2),
                                                                                                                            round(z, 2),
                                                                                                                            round(rotation_y, 2),
                                                                                                                            round(0, 2),
                                                                                                                            round(0, 2),
                                                                                                                            round(0, 2),
                                                                                                                            round(0, 2),my_type)
            if save_track_id:
                line_all = line[:-1] + ' ' + name + ' ' + track_id + '\n'
            else:
                line_all = line[:-1] + ' ' + name + '\n'

            # store the label
            # fp_label = open(self.label_save_dir + name + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt', 'a')
            fp_label = open(file_name, 'a')
            fp_label.write(line)
            fp_label.close()

            # fp_label_all.write(line_all)

        # fp_label_all.close()

    def save_pose(self, frame, file_idx, frame_idx):
        """ Save self driving car (SDC)'s own pose

        Note that SDC's own pose is not included in the regular training of KITTI dataset
        KITTI raw dataset contains ego motion files but are not often used
        Pose is important for algorithms that takes advantage of the temporal information

        equivilent to extrinsicsgt of vkitti, where aach line consists of the frame index in the video (starts from 0) followed by the row-wise flattened 4×4 extrinsic matrix at that frame:
            r1,1 r1,2 r1,3 t1
        M = r2,1 r2,2 r2,3 t2
            r3,1 r3,2 r3,3 t3
           0     0     0    1
        """

        # pose = np.array(frame.pose.transform).reshape(4,4)
        # np.savetxt(join(self.pose_save_dir, self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt'), pose)
        pose = np.array(frame.pose.transform).reshape(1,16)
        pose = np.insert(pose, 0, frame_idx, axis=1)
        file_name = join(self.pose_save_dir, self.prefix + str(file_idx).zfill(4) +'_clone'+ '.txt')

        if not isfile(file_name):
            with open(file_name, 'a') as f:
                f.write('frame r1,1 r1,2 r1,3 t1 r2,1 r2,2 r2,3 t2 r3,1 r3,2 r3,3 t3 0 0.1 0.2 1\n')
                f.close()

        with open(file_name, "ab") as f:
            # np.savetxt(f, pose, fmt='%1.7f', newline='\n')
            np.savetxt(f, pose, fmt=' '.join(['%i'] + ['%1.7f']*12 +['%i']*4), newline='\n')
            f.close()

    def save_2D_semantic(self, frame, file_idx, frame_idx, frame_obj_id, segment_class):

        """ parse and save the front camera's instance-level segmentation images in png format
                :param frame: open dataset frame proto
                :param file_idx: the current file number
                :param frame_idx: the current frame number
                :return: global_id_label_concat: A numpy segmentation map annoated by global id
        """
        frames_with_seg = []
        sequence_id = None

        # Save frames which contain CameraSegmentationLabel messages. We assume that
        # if the first image has segmentation labels, all images in this frame will.
        if frame.images[0].camera_segmentation_label.panoptic_label:
        # print(frame.images[0].camera_segmentation_label)
            frames_with_seg.append(frame)


        # if sequence_id is None:
        #   sequence_id = frame.images[0].camera_segmentation_label.sequence_id
        # # Collect 3/5 frames for this demo. However, any number can be used in practice.
        # if frame.images[0].camera_segmentation_label.sequence_id != sequence_id or len(frames_with_seg) > 4:
        #   break

        camera_front_only = [open_dataset.CameraName.FRONT]

        segmentation_protos_ordered = []
        for frame in frames_with_seg:
            segmentation_proto_dict = {image.name : image.camera_segmentation_label for image in frame.images}
            segmentation_protos_ordered.append([segmentation_proto_dict[name] for name in camera_front_only])

            # The dataset provides tracking for instances between cameras and over time.
            # By setting remap_values=True, this function will remap the instance IDs in
            # each image so that instances for the same object will have the same ID between
            # different cameras and over time.
            segmentation_protos_flat = sum(segmentation_protos_ordered, [])
            panoptic_labels, is_tracked_masks, panoptic_label_divisor = camera_segmentation_utils.decode_multi_frame_panoptic_labels_from_protos(
                segmentation_protos_flat, remap_values=True
            )

            # print('panoptic_labels:',len(panoptic_labels),'at frame', frame_idx+1)

            # We can further separate the semantic and instance labels from the panoptic
            # labels.
            NUM_CAMERA_FRAMES = 1
            semantic_labels_multiframe = []
            instance_labels_multiframe = []
            semantic_labels = []
            instance_labels = []

            pvp_folder_path = self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '/clone/'

            if not isdir(pvp_folder_path):
                os.makedirs(pvp_folder_path)

            for i in range(0, len(segmentation_protos_flat), NUM_CAMERA_FRAMES):
                semantic_labels = []
                instance_labels = []
                for j in range(NUM_CAMERA_FRAMES):
                    semantic_label, instance_label = camera_segmentation_utils.decode_semantic_and_instance_labels_from_panoptic_label(panoptic_labels[i + j], panoptic_label_divisor)
                    semantic_labels.append(semantic_label)
                    instance_labels.append(instance_label)
                semantic_labels_multiframe.append(semantic_labels)
                instance_labels_multiframe.append(instance_labels)

                # Pad labels to a common size so that they can be concatenated.
                instance_labels = [[self._pad_to_common_shape(label) for label in instance_labels] for instance_labels in instance_labels_multiframe]
                semantic_labels = [[self._pad_to_common_shape(label) for label in semantic_labels] for semantic_labels in semantic_labels_multiframe]
                instance_labels = [np.concatenate(label, axis=1) for label in instance_labels]
                semantic_labels = [np.concatenate(label, axis=1) for label in semantic_labels]

                instance_label_concat = np.concatenate(instance_labels, axis=0)
                semantic_label_concat = np.concatenate(semantic_labels, axis=0)
                panoptic_label_rgb = camera_segmentation_utils.panoptic_label_to_rgb(
                    semantic_label_concat, instance_label_concat)
                semantic_label_rgb = camera_segmentation_utils.semantic_label_to_rgb(
                    semantic_label_concat)

                sequence_id = frame.images[0].camera_segmentation_label.sequence_id
                remapped_instance_ids = camera_segmentation_utils._remap_global_ids([frame.images[0].camera_segmentation_label])
                
                # Switch key and value, from global_id:instance_id to instance_id:global_id
                # Here the global_id is the unique tracking_id in virtual KITTI !!

                remapped_instance_ids_switch = {value: key for key, value in remapped_instance_ids[sequence_id].items()}
                global_id_label_concat = np.vectorize(remapped_instance_ids_switch.get)(instance_label_concat)
                global_id_label_concat = np.nan_to_num(np.array(global_id_label_concat,dtype=float)).astype(int) # Convert None into nan by float, then convert nan to 0 by int
                # convert ndarray global_id_label_concat into, instance_labels-like, list mode
                
                # global_id_labels = global_id_label_concat.tolist()
                global_label_rgb = camera_segmentation_utils.panoptic_label_to_rgb(
                    semantic_label_concat, global_id_label_concat)


            # plt.figure(figsize=(16, 15))
            # plt.imshow(tf.image.decode_jpeg(frame.images[0].image))

            query_id_1 = 9
            query_id_2 = 10
            query_id_3 = 21
            query_id_4 = 22

            # Car 2; Truck 3;  Pedestrain 9
            query_class_1 = 2
            query_class_2 = 9
            query_class_3 = 3
            query_class_4 = 4
            query_class_5 = 5
            query_class_6 = 6
            query_class_7 = 7
            query_class_8 = 8
            query_class_9 = 10
            query_class_10 = 11
            query_class_11 = 16


            # # Find segmentation for instance ID
            # mask_id = instance_label_concat.copy()
            # mask_id = mask_id.reshape(mask_id.shape[0],mask_id.shape[1])
            # # print(mask_id.shape)
            # mask_id_3d = np.stack((mask_id,mask_id,mask_id),axis=2) #3 channel mask
            # mask_id_3d_mod = np.where(mask_id_3d==query_id, 1, 0)

            # Find segmentation for global ID
            mask_id = global_id_label_concat.copy()
            mask_id = mask_id.reshape(mask_id.shape[0],mask_id.shape[1])
            # print(mask_id.shape)
            mask_id_3d = np.stack((mask_id,mask_id,mask_id),axis=2) #3 channel mask
            mask_id_3d_mod = np.where((mask_id_3d==query_id_1) | (mask_id_3d==query_id_2)| (mask_id_3d==query_id_3)| (mask_id_3d==query_id_4), 1, 0)

            # Find class
            mask_class = semantic_label_concat.copy()
            mask_class = mask_class.reshape(mask_class.shape[0],mask_class.shape[1])
            # print(mask_class.shape)
            mask_class_3d = np.stack((mask_class,mask_class,mask_class),axis=2) #3 channel mask
            mask_class_3d_mod = np.where((mask_class_3d==query_class_1) |
                                          (mask_class_3d==query_class_2) |
                                            (mask_class_3d==query_class_3) |
                                              (mask_class_3d==query_class_4) |
                                                (mask_class_3d==query_class_5) |
                                                (mask_class_3d==query_class_6) |
                                                (mask_class_3d==query_class_7) |
                                                (mask_class_3d==query_class_8) |
                                                (mask_class_3d==query_class_9) |
                                                (mask_class_3d==query_class_10) |
                                                (mask_class_3d==query_class_11), 1, 0)
            # mask_class_3d_mod = mask_class_3d

            # new_panoptic_label_rgb = panoptic_label_rgb
            new_panoptic_label_rgb = global_label_rgb * mask_id_3d_mod * mask_class_3d_mod
            # new_panoptic_label_rgb = panoptic_label_rgb * mask_id_3d_mod * mask_class_3d_mod
            # plt.imshow(new_panoptic_label_rgb, alpha=0.3)

            # pvp_path = self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.png'
            pvp_path = pvp_folder_path + str(frame_idx).zfill(5) + '.png'
            print(pvp_path)
            # img = cv2.imdecode(np.frombuffer(img.image, np.uint8), cv2.IMREAD_COLOR)
            # rgb_img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            # plt.imsave(pvp_path, panoptic_label_rgb, format='png')
            plt.imsave(pvp_path, global_label_rgb[:,:,:3], format='png') # remove alpha channel

            # plt.grid(False)
            # plt.axis('off')
            # plt.show()

            # print('semantic_label: ',list(set(sorted(semantic_label_concat.reshape(-1).tolist())))[1:])
            
            ## All objects!
            # frame_instance_id = list(set(sorted((instance_label_concat*mask_class_3d_mod).reshape(-1).tolist())))[1:]
            # Only car objects!
            # frame_instance_id = list(set(sorted((instance_label_concat*mask_class_3d_mod).reshape(-1).tolist())))[1:]
            # print('instance_label: ',frame_instance_id)
            frame_global_id = list(set(sorted((global_id_label_concat*mask_class_3d_mod).reshape(-1).tolist())))[1:]

            # frame_semantic_class = list(set(sorted(semantic_label_concat.reshape(-1).tolist())))[1:]
            frame_semantic_class = [_ for _ in range(29) if (_!= query_class_1) and (_!= query_class_2)]

            # print(frame_obj_id)
            # print(segment_class)
        
            # with open(cur_det_file, 'a') as f:
            print(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + '.txt')
            with open(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + '.txt', 'a') as f:
                        # fp_label = open(self.label_save_dir + name + '/' + self.prefix + str(file_idx).zfill(3) + str(frame_idx).zfill(3) + '.txt', 'a')
                if os.path.getsize(self.pvp_save_dir + '/' + self.prefix + str(file_idx).zfill(4) + '_clone_scenegt_rgb_encoding' + '.txt') == 0:
                    print('Category(:id) r g b',file=f)
                for class_indice in frame_semantic_class:
                    class_name = self.class_list[class_indice]
                    r = self.class_color_list[class_indice][0]
                    g = self.class_color_list[class_indice][1]
                    b = self.class_color_list[class_indice][2]
                    if (class_indice not in segment_class) and (class_indice!= query_class_1) and (class_indice!= query_class_2):
                        print('%s %d %d %d'% (class_name, r, g, b), file=f)
                    segment_class.append(class_indice)
                for id in frame_global_id:
                    id_index_0 = np.where(global_id_label_concat == id)[0][0]
                    id_index_1 = np.where(global_id_label_concat == id)[1][0]
                    class_name = self.class_list[semantic_label_concat[id_index_0,id_index_1,0]]
                    r = global_label_rgb[id_index_0,id_index_1][0]
                    g = global_label_rgb[id_index_0,id_index_1][1]
                    b = global_label_rgb[id_index_0,id_index_1][2]
                    # print(self.class_list[semantic_label_concat[id_index_0,id_index_1,0]],':',id,' '.join(map(str, panoptic_label_rgb[id_index_0,id_index_1])))
                    if id not in frame_obj_id:
                        print('%s:%d %d %d %d'% (class_name, id, r, g, b), file=f)
                        frame_obj_id.append(id)
                    else:
                        pass
        return global_id_label_concat
    
    def create_folder(self):
        # for d in [self.label_all_save_dir, self.calib_save_dir, self.point_cloud_save_dir, self.pose_save_dir]:
        for d in [self.calib_save_dir, self.pose_save_dir, self.pvp_save_dir]:
            if not isdir(d):
                os.makedirs(d)
        for d in [self.label_save_dir, self.image_save_dir]:
            # for i in range(5):
            for i in range(1):
                if not isdir(d):
                    # os.makedirs(d + str(i))
                    os.makedirs(d)

    def convert_range_image_to_point_cloud(self,
                                           frame,
                                           range_images,
                                           camera_projections,
                                           range_image_top_pose,
                                           ri_index=0):
        """Convert range images to point cloud.
        Args:
          frame: open dataset frame
           range_images: A dict of {laser_name, [range_image_first_return,
             range_image_second_return]}.
           camera_projections: A dict of {laser_name,
             [camera_projection_from_first_return,
             camera_projection_from_second_return]}.
          range_image_top_pose: range image pixel pose for top lidar.
          ri_index: 0 for the first return, 1 for the second return.
        Returns:
          points: {[N, 3]} list of 3d lidar points of length 5 (number of lidars).
          cp_points: {[N, 6]} list of camera projections of length 5
            (number of lidars).
        """
        calibrations = sorted(frame.context.laser_calibrations, key=lambda c: c.name)
        points = []
        cp_points = []
        intensity = []

        frame_pose = tf.convert_to_tensor(
            value=np.reshape(np.array(frame.pose.transform), [4, 4]))
        # [H, W, 6]
        range_image_top_pose_tensor = tf.reshape(
            tf.convert_to_tensor(value=range_image_top_pose.data),
            range_image_top_pose.shape.dims)
        # [H, W, 3, 3]
        range_image_top_pose_tensor_rotation = transform_utils.get_rotation_matrix(
            range_image_top_pose_tensor[..., 0], range_image_top_pose_tensor[..., 1],
            range_image_top_pose_tensor[..., 2])
        range_image_top_pose_tensor_translation = range_image_top_pose_tensor[..., 3:]
        range_image_top_pose_tensor = transform_utils.get_transform(
            range_image_top_pose_tensor_rotation,
            range_image_top_pose_tensor_translation)
        for c in calibrations:
            range_image = range_images[c.name][ri_index]
            if len(c.beam_inclinations) == 0:  # pylint: disable=g-explicit-length-test
                beam_inclinations = range_image_utils.compute_inclination(
                    tf.constant([c.beam_inclination_min, c.beam_inclination_max]),
                    height=range_image.shape.dims[0])
            else:
                beam_inclinations = tf.constant(c.beam_inclinations)

            beam_inclinations = tf.reverse(beam_inclinations, axis=[-1])
            extrinsic = np.reshape(np.array(c.extrinsic.transform), [4, 4])

            range_image_tensor = tf.reshape(
                tf.convert_to_tensor(value=range_image.data), range_image.shape.dims)
            pixel_pose_local = None
            frame_pose_local = None
            if c.name == dataset_pb2.LaserName.TOP:
                pixel_pose_local = range_image_top_pose_tensor
                pixel_pose_local = tf.expand_dims(pixel_pose_local, axis=0)
                frame_pose_local = tf.expand_dims(frame_pose, axis=0)
            range_image_mask = range_image_tensor[..., 0] > 0

            # No Label Zone
            if filter_no_label_zone_points:
                nlz_mask = range_image_tensor[..., 3] != 1.0  # 1.0: in NLZ
                # print(range_image_tensor[range_image_tensor[..., 3] == 1.0])
                range_image_mask = range_image_mask & nlz_mask

            range_image_cartesian = range_image_utils.extract_point_cloud_from_range_image(
                tf.expand_dims(range_image_tensor[..., 0], axis=0),
                tf.expand_dims(extrinsic, axis=0),
                tf.expand_dims(tf.convert_to_tensor(value=beam_inclinations), axis=0),
                pixel_pose=pixel_pose_local,
                frame_pose=frame_pose_local)

            range_image_cartesian = tf.squeeze(range_image_cartesian, axis=0)
            points_tensor = tf.gather_nd(range_image_cartesian,
                                         tf.compat.v1.where(range_image_mask))

            cp = camera_projections[c.name][ri_index]
            cp_tensor = tf.reshape(tf.convert_to_tensor(value=cp.data), cp.shape.dims)
            cp_points_tensor = tf.gather_nd(cp_tensor,
                                            tf.compat.v1.where(range_image_mask))
            points.append(points_tensor.numpy())
            cp_points.append(cp_points_tensor.numpy())

            intensity_tensor = tf.gather_nd(range_image_tensor,
                                            tf.where(range_image_mask))
            intensity.append(intensity_tensor.numpy()[:, 1])

        return points, cp_points, intensity


    # def get_intensity(self, frame, range_images, ri_index=0):
    #     """Convert range images to point cloud.
    #     Args:
    #       frame: open dataset frame
    #        range_images: A dict of {laser_name,
    #          [range_image_first_return, range_image_second_return]}.
    #        camera_projections: A dict of {laser_name,
    #          [camera_projection_from_first_return,
    #           camera_projection_from_second_return]}.
    #       range_image_top_pose: range image pixel pose for top lidar.
    #       ri_index: 0 for the first return, 1 for the second return.
    #     Returns:
    #       intensity: {[N, 1]} list of intensity of length 5 (number of lidars).
    #     """
    #     calibrations = sorted(frame.context.laser_calibrations, key=lambda c: c.name)
    #     intensity = []
    #     for c in calibrations:
    #         range_image = range_images[c.name][ri_index]
    #         range_image_tensor = tf.reshape(
    #             tf.convert_to_tensor(range_image.data), range_image.shape.dims)
    #         range_image_mask = range_image_tensor[..., 0] > 0
    #         intensity_tensor = tf.gather_nd(range_image_tensor,
    #                                         tf.where(range_image_mask))
    #         intensity.append(intensity_tensor.numpy()[:, 1])
    #
    #     return intensity

    def cart_to_homo(self, mat):
        ret = np.eye(4)
        if mat.shape == (3, 3):
            ret[:3, :3] = mat
        elif mat.shape == (3, 4):
            ret[:3, :] = mat
        else:
            raise ValueError(mat.shape)
        return ret


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('load_dir', help='Directory to load Waymo Open Dataset tfrecords')
    parser.add_argument('save_dir', help='Directory to save converted KITTI-format data')
    parser.add_argument('--prefix', default='', help='Prefix to be added to converted file names')
    parser.add_argument('--num_proc', default=1, help='Number of processes to spawn')
    args = parser.parse_args()

    converter = WaymoToKITTI(args.load_dir, args.save_dir, args.prefix, args.num_proc)
    converter.convert()
