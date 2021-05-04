# import from common libraries
import numpy as np
import scipy
from scipy.stats import multivariate_normal

from yolact_detector import detector

import rclpy
from rclpy.node import Node

from sensor_msgs.msg import Image, PointCloud2 
from nav2_dynamic_msgs.msg import Obstacle, ObstacleArray
from geometry_msgs.msg import Pose, Point

class Detectron2Detector(Node):
    '''use Detectron2 to detect object masks from 2D image and estimate 3D position with Pointcloud2 data
    '''
    def __init__(self):
        super().__init__('detectron_node')
        self.declare_parameters(
            namespace='',
            parameters=[
                ('detectron_config_file', "COCO-InstanceSegmentation/mask_rcnn_R_50_FPN_3x.yaml"),
                ('detectron_score_thresh', 0.8),
                ('pointcloud2_topic', "/realsense/camera/pointcloud"),
                ('pc_downsample_factor', 16),
                ('min_mask', 20),
                ('categories', [0]),
                ('nms_filter', 0.3),
                ('outlier_thresh', 0.5)
            ])
        self.pc_downsample_factor = int(self.get_parameter("pc_downsample_factor")._value)
        self.min_mask = self.get_parameter("min_mask")._value
        self.categories = self.get_parameter("categories")._value
        self.nms_filter = self.get_parameter("nms_filter")._value
        self.outlier_thresh = self.get_parameter("outlier_thresh")._value

        # setup detectron model
        self.predictor = detector

        # subscribe to sensor 
        self.subscription = self.create_subscription(
            PointCloud2,
            self.get_parameter("pointcloud2_topic")._value,
            self.callback,
            1)

        # setup publisher
        self.detect_obj_pub = self.create_publisher(ObstacleArray, 'detection', 2)
        self.detect_img_pub = self.create_publisher(Image, 'image', 2)

        self.count = -1

    def outlier_filter(self, x, y, z, idx):
        '''simple outlier filter, assume Gaussian distribution and drop points with low probability (too far away from center)'''
        mean = [np.mean(x), np.mean(y), np.mean(z)]
        cov = np.diag(np.maximum([np.var(x), np.var(y), np.var(z)], [0.01, 0.01, 0.01]))
        rv = multivariate_normal(mean, cov)
        points = np.dstack((x, y, z))
        p = rv.pdf(points)
        return idx[p > self.outlier_thresh]

    def callback(self, msg):
        # check if there is subscirbers
        if self.detect_obj_pub.get_subscription_count() == 0 and self.detect_img_pub.get_subscription_count() == 0:
            return

        # extract data from msg
        height = msg.height
        width = msg.width
        points = np.array(msg.data, dtype = 'uint8')

        # decode rgb image
        rgb_offset = msg.fields[3].offset
        point_step = msg.point_step
        r = points[rgb_offset::point_step]
        g = points[(rgb_offset+1)::point_step]
        b = points[(rgb_offset+2)::point_step]
        img = np.concatenate([r[:, None], g[:, None], b[:, None]], axis = -1)
        self.img = img.reshape((height, width, 3))

        # decode point cloud data
        if msg.fields[0].datatype < 3:
            byte = 1
        elif msg.fields[0].datatype < 5:
            byte = 2
        elif msg.fields[0].datatype < 8:
            byte = 4
        else:
            byte = 8
        points = points.view('<f' + str(byte))
        x = points[0::int(self.pc_downsample_factor * point_step / byte)]
        y = points[1::int(self.pc_downsample_factor * point_step / byte)]
        z = points[2::int(self.pc_downsample_factor * point_step / byte)]

        self.points = [x, y, z]
        self.header = msg.header

        # call detect function
        self.detect()

    def process_points(self, pred):
        '''estimate 3D position and size with detectron output and pointcloud data'''
        x, y, z = self.points

        # map mask to point cloud data
        num_classes = pred["class"].shape[0]
        if num_classes == 0:
            return []

        masks = pred["mask"].numpy().astype('uint8').reshape((num_classes, -1))[:, ::self.pc_downsample_factor]
        # scores = pred["score"].numpy().astype(np.float)

        # estimate 3D position with simple averaging of obstacle's points
        detections = []
        for i in range(num_classes):
            # if user does not specify any interested category, keep all; else select those interested objects
            if (len(self.categories) == 0) or (pred["class"][i] in self.categories):
                idx = np.where(masks[i])[0]
                idx = self.outlier_filter(x[idx], y[idx], z[idx], idx)
                if idx.shape[0] < self.min_mask:
                    continue
                obstacle_msg = Obstacle()
                # pointcloud2 data has a different coordinate, swap y and z
                # use (max+min)/2 can avoid the affect of unbalance of points density instead of average
                x_max = x[idx].max()
                x_min = x[idx].min()
                y_max = y[idx].max()
                y_min = y[idx].min()
                z_max = z[idx].max()
                z_min = z[idx].min()
                obstacle_msg.score = np.float(pred["score"][i])
                obstacle_msg.position.x = np.float((x_max + x_min) / 2)
                obstacle_msg.position.y = np.float((y_max + y_min) / 2)
                obstacle_msg.position.z = np.float((z_max + z_min) / 2)
                obstacle_msg.size.x = np.float(x_max - x_min)
                obstacle_msg.size.y = np.float(y_max - y_min)
                obstacle_msg.size.z = np.float(z_max - z_min)
                detections.append(obstacle_msg)

        return detections

    def detect(self):
        # call detectron2 model
        pred = self.predictor.predict(self.img)

        # process pointcloud to get 3D position and size
        detections = self.process_points(pred)

        # publish detection result 
        obstacle_array = ObstacleArray()
        obstacle_array.header = self.header
        if self.detect_obj_pub.get_subscription_count() > 0:
            obstacle_array.obstacles = detections
            self.detect_obj_pub.publish(obstacle_array)

        # visualize detection with detectron API
        if self.detect_img_pub.get_subscription_count() > 0:
            out_img_msg = Image()
            out_img_msg.header = self.header
            out_img_msg.height = pred["img"].shape[0]
            out_img_msg.width = pred["img"].shape[1]
            out_img_msg.encoding = 'rgb8'
            out_img_msg.step = 3 * pred["img"].shape[1]
            out_img_msg.data = pred["img"].flatten().tolist()
            self.detect_img_pub.publish(out_img_msg)
        
def main():
    rclpy.init(args = None)
    node = Detectron2Detector()
    node.get_logger().info("start spining detectron_node...")
    
    rclpy.spin(node)

    rclpy.shutdown()

if __name__ == '__main__':
    main()
